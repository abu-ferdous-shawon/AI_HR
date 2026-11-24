"""Collect metadata about one chat turn without saving record or filter values."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.frappe_client.client import DATABASE_ERRORS, VALIDATION_ERRORS, FrappeError
from app.knowledge_packs.redactor import current_policy

_FIELD_ARGUMENTS = {"aggregate_field", "group_by"}
_AGGREGATIONS = {"count", "sum", "avg", "min", "max"}
_OPERATORS = {"=", "!=", "<", "<=", ">", ">=", "like", "not like", "in", "not in", "between", "is"}


def _columns(fields: list[dict]) -> list[dict]:
    policy = current_policy()
    return [{key: row[key] for key in ("fieldname", "fieldtype", "reqd") if key in row}
            for row in fields if not policy.blocked(row["fieldname"])]


@dataclass
class TurnTrace:
    intent: str = "chat"
    date_range: tuple[str, str] | None = None
    follow_up: bool = False
    matched_doctypes: list[str] = field(default_factory=list)
    matched_reports: list[str] = field(default_factory=list)
    schemas: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    retry_count: int = 0
    error_trace: list[dict] = field(default_factory=list)

    def retrieval(self, context) -> None:
        policy = current_policy()
        self.matched_doctypes = [name for name in context.matched_doctypes or context.schemas
                                 if not policy.blocked(name)]
        self.matched_reports = [name for name in context.matched_reports or context.reports
                                if not policy.blocked(name)]
        self.schemas = {name: _columns(schema["fields"]) for name, schema in context.schemas.items()
                        if not policy.blocked(name)}
        self.reports = {name: _columns(report["filters"]) for name, report in context.reports.items()
                        if not policy.blocked(name)}

    def arguments(self, args: dict, declared: dict) -> dict:
        policy = current_policy()
        doctype, report = args.get("doctype"), args.get("report_name")
        doctype = doctype if isinstance(doctype, str) and doctype in self.schemas else None
        report = report if isinstance(report, str) and report in self.reports else None
        fields = {row["fieldname"] for row in self.schemas.get(doctype, [])}
        report_fields = {row["fieldname"] for row in self.reports.get(report, [])}
        clean = {}
        for key, value in args.items():
            if key not in declared or policy.blocked(key):
                continue
            if key == "doctype" and doctype:
                clean[key] = doctype
            elif key == "report_name" and report:
                clean[key] = report
            elif key == "fields" and isinstance(value, list):
                clean[key] = [name for name in value[:18] if isinstance(name, str) and name in fields]
            elif key in _FIELD_ARGUMENTS and isinstance(value, str) and value in fields:
                clean[key] = value
            elif key == "aggregate_fn" and isinstance(value, str) and value in _AGGREGATIONS:
                clean[key] = value
            elif key in {"limit", "start"} and type(value) is int and 0 <= value <= 500:
                clean[key] = value
            elif key in {"filters", "or_filters"} and isinstance(value, list):
                clean[key] = [{"field": row["field"], "op": row["op"], "value": "[redacted]"}
                              for row in value[:12] if isinstance(row, dict)
                              and isinstance(row.get("field"), str) and row["field"] in fields
                              and isinstance(row.get("op"), str) and row["op"] in _OPERATORS]
            elif key == "filters" and isinstance(value, dict):
                clean[key] = {name: "[redacted]" for name in value if name in report_fields}
            else:
                clean[key] = "[redacted]"
        return clean

    def record_tool(self, tool: str, args: dict, outcome: str, latency_ms: float,
                    failure: FrappeError | None = None, *, status: int | None = None,
                    summary: dict | None = None) -> None:
        entry = {"tool": tool, "args": args, "ok": outcome == "ok", "kind": outcome,
                 "latency_ms": latency_ms, "retry": self.retry_count, **(summary or {})}
        if failure:
            entry["http_status"] = failure.status_code
            entry["error_type"] = (failure.error_type if failure.error_type in
                                   VALIDATION_ERRORS | DATABASE_ERRORS | {"PermissionError"} else "FrappeError")
        elif status is not None:
            entry["http_status"] = status
        self.tool_calls.append(entry)
        if outcome not in {"ok", "skipped"}:
            self.error("tool", outcome, **{key: entry[key] for key in
                                           ("tool", "http_status", "error_type", "retry") if key in entry})

    def error(self, stage: str, outcome: str, **labels) -> None:
        self.error_trace.append({"stage": stage, "outcome": outcome, **labels})

    def query_summary(self, *, verified_only: bool = False) -> dict:
        return {"intent": self.intent, "follow_up": self.follow_up,
                "date_range": list(self.date_range) if self.date_range else None,
                "doctypes": list(self.schemas) if verified_only else self.matched_doctypes,
                "reports": list(self.reports) if verified_only else self.matched_reports}


_ACTIVE: ContextVar[TurnTrace | None] = ContextVar("chat_turn_trace", default=None)


def current_trace() -> TurnTrace | None:
    return _ACTIVE.get()


@contextmanager
def capture_turn(trace: TurnTrace):
    token = _ACTIVE.set(trace)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
