import importlib
import logging
import pkgutil
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from app.frappe_client.client import FrappeError
from app.auth.security import AccessDenied, ToolInputError, UserContext, is_hr_admin, scrub
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


_TOOLS: dict[str, Tool] = {}

# How the last run_tool call ended: ok, denied, invalid, failed, unknown_tool.
# Read once with take_outcome(). Only the audit log uses it; the model never sees it.
_outcome: ContextVar[str | None] = ContextVar("tool_outcome", default=None)


def take_outcome() -> str | None:
    kind = _outcome.get()
    _outcome.set(None)
    return kind


def tool(name: str, description: str, parameters: dict | None = None,
         allow: tuple[str, ...] = (), hr_only: bool = False):
    """Decorator that adds a function to the list of approved tools."""
    def decorator(func):
        if name in _TOOLS:
            raise ValueError(f"Tool '{name}' is registered twice.")
        _TOOLS[name] = Tool(
            name, description,
            parameters or {"type": "object", "properties": {}}, func,
            frozenset(allow), hr_only,
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


def get_tool_specs() -> list[dict]:
    """What we show the model: names, descriptions and inputs only."""
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in _TOOLS.values()
    ]


def run_tool(name: str, args: dict | None, ctx: UserContext) -> dict:
    """Run one tool safely. Always returns {'result': ...} or {'error': ...}."""
    selected = _TOOLS.get(name)
    if selected is None:
        _outcome.set("unknown_tool")
        return {"error": f"Unknown tool '{name}'."}

    # Keep only inputs the tool declared. Anything else is silently dropped.
    allowed = set(selected.parameters.get("properties", {}))
    clean = {k: v for k, v in (args or {}).items() if k in allowed}

    try:
        if selected.hr_only and not is_hr_admin(ctx):
            _outcome.set("denied")
            return {"error": HR_ONLY_MESSAGE}
        value = current_policy().sanitize(selected.func(ctx, **clean))
        value = scrub(value, selected.allow)
        _outcome.set("ok")
        return {"result": value}
    except AccessDenied as exc:
        _outcome.set("denied")
        return {"error": str(exc)}
    except ToolInputError as exc:
        _outcome.set("invalid")
        return {"error": str(exc)}
    except FrappeError as exc:
        _outcome.set("failed")
        log.warning("Frappe error in %s (HTTP %s)", name, exc.status_code)
        return {"error": "The HR system could not complete this request."}
    except Exception as exc:
        _outcome.set("failed")
        log.error("Unexpected error in tool %s (%s)", name, type(exc).__name__)
        return {"error": "Something went wrong while running this tool."}
