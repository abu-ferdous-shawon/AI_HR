"""Audit trail and JSON logs. The audit log never holds raw personal values."""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings
from app.core.masking import TYPED_SECRET_RE

MB = 1024 * 1024
MAX_VALUE = 60
# Model-supplied arguments that are safe to log as they are. Everything else
# (employee names, search words, calculations) is replaced by its length.
SAFE_ARGS = {
    "record_type", "from_date", "to_date", "status", "scope", "limit", "aggregation",
    "group_by", "report_type", "file_format", "year", "month", "metric", "source",
    "include_weekly_off", "employee_id",
}
ROW_LISTS = ("records", "payslips", "applications", "holidays", "balances",
             "excerpts", "groups")

_audit = logging.getLogger("hr_ai.audit")
_audit.setLevel(logging.INFO)
_audit.propagate = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"time": datetime.fromtimestamp(record.created, timezone.utc)
                .isoformat(timespec="milliseconds"),
                "level": record.levelname, "logger": record.name,
                "message": record.getMessage()}
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


class _PrivateRotatingFile(RotatingFileHandler):
    """Rotating file that only its owner can read."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def _make_handler(path: str, max_bytes: int, backups: int) -> logging.Handler:
    if path in ("", "-"):
        return logging.StreamHandler(sys.stdout)
    Path(path).parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    return _PrivateRotatingFile(path, maxBytes=max_bytes, backupCount=backups,
                                encoding="utf-8")


def close() -> None:
    for handler in list(_audit.handlers):
        _audit.removeHandler(handler)
        handler.close()


def configure(path=None, *, max_bytes: int | None = None, backups: int | None = None) -> None:
    """Start writing the audit log. Defaults come from the settings."""
    close()
    handler = _make_handler(
        settings.audit_log_file if path is None else str(path),
        max_bytes or settings.audit_log_max_mb * MB,
        settings.audit_log_backups if backups is None else backups)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _audit.addHandler(handler)


def setup_app_logging() -> None:
    """JSON (or plain) application logs with rotation. Called once at startup."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = _make_handler(settings.log_file or "-", settings.log_max_mb * MB,
                            settings.log_backups)
    handler.setFormatter(JsonFormatter() if settings.log_json else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def record(event: str, **fields) -> None:
    """Write one audit line. Callers pass only IDs, counts and outcomes."""
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             "event": event, **fields}
    _audit.info(json.dumps(entry, ensure_ascii=False, default=str))


def safe_args(args) -> dict:
    out = {}
    for key, value in (args or {}).items():
        key = str(key)[:40]
        if key not in SAFE_ARGS:
            out[key] = f"<hidden:{len(str(value))} chars>" if value not in (None, "") else None
            continue
        if isinstance(value, str):
            if len(value) > MAX_VALUE or TYPED_SECRET_RE.search(value):
                value = f"<hidden:{len(value)} chars>"
        elif value is not None and not isinstance(value, (bool, int, float)):
            value = "<hidden>"
        out[key] = value
    return out


def describe_result(payload) -> dict:
    """Row count, outcome and target of a tool result, without any values."""
    if isinstance(payload, list):
        return {"rows": len(payload)}
    if not isinstance(payload, dict):
        return {}
    out: dict = {}
    rows = None
    for key in ("count", "records_counted"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            rows = value
            break
    if rows is None:
        for key in ROW_LISTS:
            if isinstance(payload.get(key), list):
                rows = len(payload[key])
                break
    file = payload.get("file")
    if rows is None and isinstance(file, dict) and isinstance(file.get("rows"), int):
        rows = file["rows"]
    if rows is None and payload.get("outcome") == "no_records":
        rows = 0
    if rows is not None:
        out["rows"] = rows
    for key in ("outcome", "scope", "record_type"):
        if isinstance(payload.get(key), str):
            out[key] = payload[key]
    if isinstance(payload.get("truncated"), bool):
        out["truncated"] = payload["truncated"]
    who = payload.get("employee")
    if isinstance(who, dict) and isinstance(who.get("id"), str):
        out["target"] = who["id"]
    elif isinstance(payload.get("employee_id"), str):
        out["target"] = payload["employee_id"]
    return out


def tool_entry(item: dict) -> dict:
    """One tool_log item as an audit entry. Error text is dropped (it can hold names)."""
    entry = {"tool": item.get("tool"), "args": safe_args(item.get("args")),
             "ok": bool(item.get("ok")), "kind": item.get("kind")}
    for key in ("rows", "outcome", "truncated", "scope", "record_type", "target"):
        if key in item:
            entry[key] = item[key]
    return entry