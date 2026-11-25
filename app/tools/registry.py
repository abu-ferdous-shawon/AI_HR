import importlib
import logging
import pkgutil
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable
from pydantic import BaseModel, ValidationError

from app.frappe_client.client import FrappeError
from app.auth.security import AccessDenied, ToolInputError, UserContext, is_hr_admin, require_employee, scrub
from app.knowledge_packs.redactor import current_policy

log = logging.getLogger("hr_ai.tools")

HR_ONLY_MESSAGE = "This information is available to HR only."


@dataclass(frozen=True)
class Tool:
    name: str
    description: str   # the model reads this to decide when to use the tool
    parameters: dict   # JSON-schema style list of inputs the MODEL may supply
    func: Callable[..., Any]
    allow: frozenset = frozenset()   # confidential fields it may return ("*" = all; the agent masks them)
    hr_only: bool = False            # only HR may use it; enforced in run_tool
    input_model: type[BaseModel] | None = None
    requires_employee: bool = False


_TOOLS: dict[str, Tool] = {}

# How the last run_tool call ended: ok, denied, invalid, failed, unknown_tool.
@dataclass(frozen=True)
class ToolExecution:
    kind: str | None = None
    failure: FrappeError | None = None


_outcome: ContextVar[ToolExecution] = ContextVar("tool_outcome", default=ToolExecution())


def take_execution() -> ToolExecution:
    execution = _outcome.get()
    _outcome.set(ToolExecution())
    return execution


def take_outcome() -> str | None:
    return take_execution().kind


def _finish(kind: str, payload: dict, failure: FrappeError | None = None) -> dict:
    _outcome.set(ToolExecution(kind, failure))
    return payload


def tool(name: str, description: str, parameters: dict | None = None,
         allow: tuple[str, ...] = (), hr_only: bool = False, *,
         input_model: type[BaseModel] | None = None, requires_employee: bool = False):
    """Decorator that adds a function to the list of approved tools."""
    def decorator(func):
        if name in _TOOLS:
            raise ValueError(f"Tool '{name}' is registered twice.")
        _TOOLS[name] = Tool(
            name, description,
            input_model.model_json_schema() if input_model else
            (parameters or {"type": "object", "properties": {}}), func,
            frozenset(allow), hr_only, input_model, requires_employee,
        )
        return func
    return decorator


def load_all_tools() -> None:
    """Import every module under app/tools so their @tool lines run."""
    import app.tools as package
    for module in pkgutil.walk_packages(package.__path__, prefix="app.tools."):
        importlib.import_module(module.name)
        log.debug("Loaded %s", module.name)
    if not _TOOLS:
        raise RuntimeError(
            "No tools were registered. Check that app/tools/hr/profile.py exists "
            "and that every folder under app/tools has an __init__.py file."
        )


def get_tool_specs(*, include_contextual: bool = False, has_employee: bool = True) -> list[dict]:
    """What we show the model: names, descriptions and inputs only."""
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters,
         **({"json_schema": True} if t.input_model else {})}
        for t in _TOOLS.values()
        if (include_contextual or t.input_model is None) and (has_employee or not t.requires_employee)
    ]


def run_tool(name: str, args: dict | None, ctx: UserContext) -> dict:
    """Run one tool safely. Always returns {'result': ...} or {'error': ...}."""
    selected = _TOOLS.get(name)
    if selected is None:
        return _finish("unknown_tool", {"error": f"Unknown tool '{name}'."})

    # Keep only inputs the tool declared. Anything else is silently dropped.
    allowed = set(selected.parameters.get("properties", {}))
    clean = {k: v for k, v in (args or {}).items() if k in allowed}

    try:
        if selected.requires_employee:
            require_employee(ctx)
        if selected.hr_only and not is_hr_admin(ctx):
            return _finish("denied", {"error": HR_ONLY_MESSAGE})
        if selected.input_model is not None:
            arguments = selected.input_model.model_validate(args or {}, strict=True)
            value = selected.func(ctx, arguments)
        else:
            value = selected.func(ctx, **clean)
        value = current_policy().sanitize(value)
        value = scrub(value, selected.allow)
        return _finish("ok", {"result": value})
    except AccessDenied as exc:
        return _finish("denied", {"error": str(exc)})
    except ToolInputError as exc:
        return _finish("invalid", {"error": str(exc)})
    except ValidationError as exc:
        fields = ", ".join(dict.fromkeys(
            str(error["loc"][0]) for error in exc.errors(include_input=False)
            if error["loc"] and error["loc"][0] in allowed
        ))
        return _finish("invalid", {"error": f"Invalid tool arguments. Check the input schema for: {fields or 'the request'}."})
    except FrappeError as exc:
        if selected.input_model is not None:
            if exc.status_code in (401, 403) or exc.error_type == "PermissionError":
                return _finish("denied", {"error": "Frappe denied access to that record or report."}, exc)
            if exc.status_code in (400, 404, 417, 422):
                return _finish("invalid", {"error": "Frappe rejected the request. Check the record ID, fields, and filters."}, exc)
        log.warning("Frappe error in %s (HTTP %s)", name, exc.status_code)
        return _finish("failed", {"error": "The HR system could not complete this request."}, exc)
    except Exception as exc:
        log.error("Unexpected error in tool %s (%s)", name, type(exc).__name__)
        return _finish("failed", {"error": "Something went wrong while running this tool."})
