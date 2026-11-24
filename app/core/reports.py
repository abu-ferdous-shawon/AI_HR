import csv
import datetime
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from app.core.sites import current_site
from app.knowledge_packs.redactor import current_policy

REPORT_DIR = Path(tempfile.gettempdir()) / "hr_ai_reports"
TTL_SECONDS = 3600  # files are deleted after one hour


@dataclass(frozen=True)
class StoredReport:
    owner: str      # the user who asked for it; only they can download it
    site: str
    path: Path
    name: str
    created: float


_reports: dict[str, StoredReport] = {}
_lock = Lock()


def _safe_cell(value):
    """Stop spreadsheet formulas: a text starting with = + - @ would be run by Excel."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _cleanup() -> None:
    cutoff = time.time() - TTL_SECONDS
    with _lock:
        for key in [k for k, r in _reports.items() if r.created < cutoff]:
            _reports.pop(key).path.unlink(missing_ok=True)


def save_report(owner: str, title: str, columns: list[str], rows: list[list],
                file_format: str = "excel", *, source_doctype: str | None = None,
                column_fields: tuple[str, ...] | None = None) -> dict:
    if file_format not in ("excel", "csv"):
        raise ValueError("file_format must be 'excel' or 'csv'")
    if column_fields is not None and len(column_fields) != len(columns):
        raise ValueError("report column sources must match the headers")
    specs = [{"label": label, "fieldname": field}
             for label, field in zip(columns, column_fields or columns)]
    clean = current_policy().sanitize_report({"columns": specs, "rows": rows}, source_doctype)
    columns = [column["label"] for column in clean["columns"]]
    rows = clean["rows"]
    if not columns:
        raise ValueError("no report columns are available")
    _cleanup()
    REPORT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:50] or "report"
    ext = "xlsx" if file_format == "excel" else "csv"
    name = f"{slug}_{datetime.date.today().isoformat()}.{ext}"
    report_id = uuid.uuid4().hex          # unguessable, and the owner is checked too
    path = REPORT_DIR / f"{report_id}.{ext}"

    header = [_safe_cell(c) for c in columns]
    body = [[_safe_cell(c) for c in row] for row in rows]

    if file_format == "csv":
        # utf-8-sig so Excel shows Bangla correctly when opening a CSV
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(body)
    else:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter

        book = Workbook()
        sheet = book.active
        sheet.title = slug[:31]
        sheet.append(header)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in body:
            sheet.append(row)
        sheet.freeze_panes = "A2"
        for index, column in enumerate(header, start=1):
            longest = max([len(str(column))] + [len(str(r[index - 1])) for r in body[:200]])
            sheet.column_dimensions[get_column_letter(index)].width = min(longest + 2, 50)
        book.save(path)

    with _lock:
        _reports[report_id] = StoredReport(owner, current_site().name, path, name, time.time())
    return {"id": report_id, "name": name, "rows": len(body), "format": file_format}


def get_report(report_id: str, owner: str) -> StoredReport | None:
    """Return the report only if it belongs to this user and has not expired."""
    with _lock:
        stored = _reports.get(report_id)
    if (stored is None or stored.owner != owner or stored.site != current_site().name or not stored.path.exists()
            or stored.created < time.time() - TTL_SECONDS):
        return None
    return stored
