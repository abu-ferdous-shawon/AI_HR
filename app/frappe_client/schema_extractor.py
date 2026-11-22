"""Read Frappe metadata through its HTTP APIs and keep only indexable schema."""
import re
from collections import defaultdict
from dataclasses import dataclass
from html import unescape

from app.frappe_client.client import MAX_LIMIT, FrappeClient, FrappeError
from app.frappe_client.report_filters import static_report_filters
from app.indexer.util import digest

LAYOUT = frozenset({
    "Section Break", "Column Break", "Tab Break", "HTML", "Fold", "Heading", "Button",
})
INTERNAL = frozenset({
    "Activity Log", "Access Log", "Comment", "Communication", "Custom Field",
    "Custom DocPerm", "Deleted Document", "DocField", "DocPerm", "DocShare",
    "DocType", "DocType Action", "DocType Link", "DocType State", "Error Log",
    "Event Streaming Settings", "File", "Integration Request", "Module Def",
    "Prepared Report", "Property Setter", "Report", "Report Column", "Report Filter",
    "Scheduled Job Log", "Scheduled Job Type", "Server Script", "Client Script",
    "Session Default", "System Settings", "User Permission", "Version", "View Log",
    "Workflow", "Workflow Document State", "Workflow Transition",
})
SECRET_FIELDS = frozenset({
    "password", "new_password", "api_key", "api_secret", "auth_token",
    "access_token", "refresh_token", "client_secret", "user_secret",
})
RELATIONS = frozenset({"Link", "Dynamic Link", "Table", "Table MultiSelect"})
OPTION_TYPES = RELATIONS | {"Select", "Currency", "Float", "Data", "Code", "Autocomplete"}
REPORT_TYPES = ("Query Report", "Script Report")
FIELDNAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


class MetadataError(RuntimeError):
    pass


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _description(value) -> str:
    text = re.sub(r"<[^>]*>", " ", unescape(_text(value)))
    return " ".join(text.split())[:2000]


def _checked(value) -> bool:
    return value is True or value == 1 or value == "1"


def _records(client, doctype: str, fields: list[str], filters=None) -> list[dict]:
    rows = []
    while True:
        try:
            page = client.get_list(doctype, fields, filters=filters, order_by="name asc",
                                   limit=MAX_LIMIT, start=len(rows))
        except FrappeError as exc:
            if exc.status_code == 403:
                raise MetadataError(f"Frappe denied the {doctype} catalog. Check the indexing account's read permissions.") from None
            raise
        if any(not all(field in row for field in fields) for row in page):
            raise MetadataError(f"Frappe omitted catalog fields for {doctype}. Check metadata read permissions.")
        rows.extend(page)
        if len(page) < MAX_LIMIT:
            return rows


@dataclass(frozen=True)
class Catalog:
    doctypes: dict[str, str]
    reports: dict[str, str]
    workflows: dict[str, list[str]]
    modules: list[str]


def read_catalog(client: FrappeClient, *, include=(), exclude=()) -> Catalog:
    """Get names and revisions, including customizations that don't touch DocType.modified."""
    blocked = (INTERNAL - set(include)) | set(exclude)
    revisions = {
        row["name"]: [row]
        for row in _records(client, "DocType", ["name", "modified", "module"])
        if row["name"] not in blocked
    }
    for source, link in (("Custom Field", "dt"), ("Property Setter", "doc_type")):
        for row in _records(client, source, ["name", "modified", link]):
            if row[link] in revisions:
                revisions[row[link]].append((source, row))

    workflows = defaultdict(list)
    for row in _records(client, "Workflow", ["name", "modified", "document_type"],
                        filters={"is_active": 1}):
        parent = row["document_type"]
        if parent in revisions:
            revisions[parent].append(("Workflow", row))
            workflows[parent].append(row["name"])

    reports = _records(
        client, "Report", ["name", "modified", "ref_doctype", "module", "report_type"],
        filters=[["disabled", "=", 0], ["report_type", "in", list(REPORT_TYPES)]],
    )
    modules = _records(client, "Module Def", ["name"])
    return Catalog(
        doctypes={name: digest(items) for name, items in revisions.items()},
        reports={row["name"]: digest(row) for row in reports if row["ref_doctype"] not in exclude},
        workflows=dict(workflows),
        modules=[row["name"] for row in modules],
    )


def _field(row: dict, denied: set[str]) -> dict | None:
    name = _text(row.get("fieldname"))
    kind = _text(row.get("fieldtype"))
    if not FIELDNAME.fullmatch(name) or name.casefold() in denied or kind in LAYOUT | {"Password"}:
        return None
    options = _text(row.get("options")) if kind in OPTION_TYPES else ""
    if kind == "Dynamic Link" and options.casefold() in denied:
        return None
    return {
        "fieldname": name, "label": _text(row.get("label")) or name.replace("_", " "),
        "fieldtype": kind, "options": options,
        "reqd": _checked(row.get("reqd")), "in_list_view": _checked(row.get("in_list_view")),
        "in_standard_filter": _checked(row.get("in_standard_filter")),
        "permlevel": int(row.get("permlevel") or 0),
    }


def read_schema(client: FrappeClient, name: str, workflows: list[str], denied=()) -> dict:
    meta = next((doc for doc in client.get_meta(name) if doc.get("name") == name), None)
    if meta is None:
        raise MetadataError(f"Frappe returned no schema for {name}")
    denied = SECRET_FIELDS | {field.casefold() for field in denied}
    fields = [item for row in meta.get("fields", []) if (item := _field(row, denied))]
    fields_by_name = {field["fieldname"]: field for field in fields}
    title = _text(meta.get("title_field"))
    status = fields_by_name.get("status", {})
    search_fields = [field for field in re.split(r"[\s,]+", _text(meta.get("search_fields")))
                     if field in fields_by_name or field == "name"]
    states = []
    for workflow in workflows:
        doc = client.get_doc("Workflow", workflow)
        state_field = _text(doc.get("workflow_state_field"))
        if state_field.casefold() not in denied:
            states.append({
                "fieldname": state_field,
                "states": [{"state": _text(row.get("state")), "doc_status": row.get("doc_status")}
                           for row in doc.get("states", []) if row.get("state")],
            })
    if not states and meta.get("states"):
        states.append({"fieldname": "", "states": [
            {"state": _text(row.get("title")), "doc_status": None}
            for row in meta["states"] if row.get("title")
        ]})
    return {
        "name": name, "label": _text(meta.get("label")) or name,
        "module": _text(meta.get("module")), "description": _description(meta.get("description")),
        "is_submittable": _checked(meta.get("is_submittable")),
        "istable": _checked(meta.get("istable")), "issingle": _checked(meta.get("issingle")),
        "title_field": title if title in fields_by_name or title == "name" else "",
        "search_fields": search_fields,
        "status_options": _options(status.get("options")) if status.get("fieldtype") == "Select" else [],
        "workflow_states": states, "fields": fields,
        "relationships": [
            {"fieldname": field["fieldname"], "kind": field["fieldtype"], "target": field["options"]}
            for field in fields if field["fieldtype"] in RELATIONS and field["options"]
        ],
    }


def _options(options) -> list[str]:
    return [value.strip() for value in _text(options).splitlines() if value.strip()]


def read_report(client: FrappeClient, name: str, denied_for) -> dict:
    doc = client.get_doc("Report", name)
    if _checked(doc.get("disabled")) or doc.get("report_type") not in REPORT_TYPES:
        raise MetadataError(f"Report {name} changed during sync. Retry the sync.")
    ref = _text(doc.get("ref_doctype"))
    report_type = doc["report_type"]
    filters = doc.get("filters") or []
    source = "Report"
    if not filters:
        try:
            details = client.call_method("frappe.desk.query_report.get_script", {"report_name": name})
        except FrappeError as exc:
            if exc.status_code not in {403, 404}:
                raise
            details = None
        script = _text(details.get("script")) if isinstance(details, dict) else _text(doc.get("javascript"))
        filters = details.get("filters") if isinstance(details, dict) else None
        if not filters:
            filters = static_report_filters(script, name)
            source = "JavaScript" if filters else ("undeclared" if details is not None else "unavailable")
    denied = SECRET_FIELDS | {field.casefold() for field in denied_for(ref)}
    cleaned = []
    for row in filters:
        item = _field(row, denied)
        if item:
            cleaned.append({key: item[key] for key in ("fieldname", "label", "fieldtype", "options", "reqd")})
    return {
        "name": name, "ref_doctype": ref, "module": _text(doc.get("module")),
        "description": _description(doc.get("description")) or f"{report_type} for {ref}",
        "report_type": report_type,
        "filters": cleaned, "filter_source": source,
    }
