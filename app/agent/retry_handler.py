"""Bounded correction of validation failures in permission-scoped Frappe reads."""
import re
from dataclasses import dataclass

from app.core.llm.base import ToolCall
from app.frappe_client.client import DATABASE_ERRORS, VALIDATION_ERRORS, FrappeError
from app.knowledge_packs.redactor import current_policy
from app.tools.registry import ToolExecution

MAX_CORRECTIONS = 2
_TARGET_ARGUMENT = {
    "query_records": "doctype", "aggregate_records": "doctype", "run_frappe_report": "report_name",
}
_FIELD_ERROR = re.compile(
    r"(Invalid field(?: ?name)?|Field not permitted in query|Unknown field|Invalid filter(?: field)?|"
    r"Missing mandatory filter|Missing required filter|Missing filter|Mandatory filter missing): "
    r"['\"]?([\w -]{1,80}?)['\"]?([.!]?)", re.IGNORECASE,
)
_UNKNOWN_COLUMN = re.compile(r"(?:\(1054, \"|1054: )?Unknown column '([^']+)' in '(field list|where clause|order clause|group statement)'\"?\)?",
                             re.IGNORECASE)
_DATE_ERROR = re.compile(
    r"(?:Invalid date(?:time)?(?: format)?|Incorrect date value|Please select a valid date filter)[.!]?",
    re.IGNORECASE,
)
_EMPHASIS = re.compile(r"</?(?:b|strong|code)>", re.IGNORECASE)


def _validation_message(error: FrappeError, fields: list[dict], target: str | None) -> str | None:
    """Forward recognized diagnostics only. Upstream text may contain record values."""
    policy = current_policy()
    names = {field[key].casefold() for field in fields for key in ("fieldname", "label")
             if isinstance(field.get(key), str) and not policy.blocked(field[key])}
    for detail in (*error.details, error.message):
        text = _EMPHASIS.sub("", detail).strip()
        if error.error_type and text.startswith(error.error_type + ": "):
            text = text.removeprefix(error.error_type + ": ")
        if _DATE_ERROR.fullmatch(text):
            return text
        if match := _FIELD_ERROR.fullmatch(text):
            field = match[2]
            return text if field.casefold() in names else f"{match[1]}: [field omitted]{match[3]}"
        if match := _UNKNOWN_COLUMN.fullmatch(text):
            column = match[1]
            field = column.removeprefix(f"tab{target}.")
            safe = column if field.casefold() in names else "[field omitted]"
            return f"Unknown column '{safe}' in '{match[2]}'."
        if re.match(r"(?:Invalid date(?: format)?|Incorrect date value|Invalid datetime(?: format)?):", text, re.I):
            return text.split(":", 1)[0] + ": [value omitted]"
        if text.endswith(" is not a valid date string."):
            return "[value omitted] is not a valid date string."
    return None


@dataclass(frozen=True)
class Correction:
    tool: str
    key: str
    target: str | None

    def accepts(self, calls: list[ToolCall]) -> bool:
        return (len(calls) == 1 and calls[0].name == self.tool
                and (self.target is None or calls[0].args.get(self.key) == self.target))


class ReadRetry:
    def __init__(self, schemas: dict, reports: dict):
        self.schemas = schemas
        self.reports = reports
        self.pending: Correction | None = None
        self.count = 0

    def begin(self) -> Correction | None:
        correction, self.pending = self.pending, None
        if correction:
            self.count += 1
        return correction

    def handle_failure(self, call: ToolCall, execution: ToolExecution, result: dict) -> str | None:
        """Schedule a model correction, or return the localized terminal response key."""
        key = _TARGET_ARGUMENT.get(call.name)
        if not key:
            return "retry" if execution.kind == "invalid" else "read_failed"
        catalog, column = ((self.reports, "filters") if key == "report_name"
                           else (self.schemas, "fields"))
        supplied = call.args.get(key)
        target = supplied if isinstance(supplied, str) and supplied in catalog else None
        policy = current_policy()
        fields = [field for field in catalog[target][column]
                  if not policy.blocked(field["fieldname"])] if target else []
        error = execution.failure
        message = _validation_message(error, fields, target) if error else None
        if error:
            fixable = (error.status_code in (400, 417, 422) and (message or error.error_type in VALIDATION_ERRORS)
                       or error.status_code == 500 and error.error_type in DATABASE_ERRORS and message)
        else:
            fixable = execution.kind == "invalid"
        if not fixable:
            return "retry" if execution.kind == "invalid" else "read_failed"
        if self.count >= MAX_CORRECTIONS:
            return "retry"
        self.pending = Correction(call.name, key, target)
        result["correction"] = {
            "attempt": self.count + 1,
            "tool": call.name,
            "target": target,
            "validation_message": message or (
                f"Frappe returned {error.error_type}." if error else result["error"]
            ),
            "verified_fields": fields,
            "instruction": ("Correct this tool call using the verified schema and the original request. "
                            "Keep the same target and preserve requested filters and scope. "
                            "Ask the user for any required filter value you cannot determine."),
        }
        return None
