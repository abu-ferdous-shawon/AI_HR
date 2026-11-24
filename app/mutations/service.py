import math
from dataclasses import dataclass
from datetime import date, datetime, time
from html import escape
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.retriever import self_filters
from app.auth.security import UserContext
from app.core.sites import current_site
from app.frappe_client.client import FIELD_NAME, FrappeClient, FrappeError
from app.frappe_client.schema_extractor import INTERNAL
from app.knowledge_packs.redactor import current_policy

Operation = Literal["create", "update", "submit", "cancel", "delete"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)]
Scalar = str | int | float | bool | None
FieldValue = Scalar | list[dict[str, Scalar]]
_PERMISSION = {"create": "create", "update": "write", "submit": "submit", "cancel": "cancel", "delete": "delete"}
_RESERVED = {
    "name", "doctype", "docstatus", "owner", "creation", "modified", "modified_by",
    "parent", "parenttype", "parentfield", "idx", "amended_from", "flags",
}
_BLOCKED_TYPES = INTERNAL | {
    "User", "Role", "Has Role", "Custom Role", "User Type", "Webhook",
    "OAuth Client", "OAuth Bearer Token", "Role Profile", "Client Script", "Server Script",
}
_TEXT = {"Data", "Small Text", "Text", "Long Text", "Text Editor", "Markdown Editor",
         "Phone", "Autocomplete", "Link", "Select"}
_NUMBER = {"Int", "Long Int", "Float", "Currency", "Percent", "Check", "Rating", "Duration"}
_TEMPORAL = {"Date": date, "Datetime": datetime, "Time": time}
_SUPPORTED = _TEXT | _NUMBER | set(_TEMPORAL) | {"Table", "Table MultiSelect"}
_MAX_TABLE_ROWS = 8


class MutationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)

    operation: Operation
    doctype: Name
    name: Name | None = None
    payload: dict[str, FieldValue] = Field(default_factory=dict, max_length=24)


class MutationFailure(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class PreparedMutation:
    request: MutationRequest
    payload: dict
    modified: str | None
    changes: tuple[dict, ...]


def _flag(value) -> bool:
    return value in (1, "1", True)


def _empty(value) -> bool:
    return value is None or value == "" or value == []


def writable_doctype(doctype: str) -> bool:
    return doctype in current_site().write_doctypes and doctype not in _BLOCKED_TYPES


def _permission(client: FrappeClient, dtype: str, ptype: str, name: str | None = None) -> None:
    if not client.has_document_permission(dtype, ptype, name):
        raise MutationFailure(403, "Frappe denied permission for this change.")


def load_mutation_schema(client: FrappeClient, doctype: str, operation: Operation) -> dict[str, dict]:
    if not client.acts_as_user or not writable_doctype(doctype):
        raise MutationFailure(403, "Chat writes are unavailable for this DocType or sign-in.")
    # Owners may have document-specific privileges. The exact operation is checked on the target
    # before a proposal exists. A create can be validated directly at the DocType level.
    _permission(client, doctype, "create" if operation == "create" else "read")
    types = {row["name"]: row for row in client.get_meta(doctype) if isinstance(row.get("name"), str)}
    root = types.get(doctype)
    if root is None or any(_flag(root.get(key)) for key in ("istable", "issingle", "is_virtual")):
        raise MutationFailure(422, "Choose a supported, regular Frappe DocType.")
    if operation in ("submit", "cancel") and not _flag(root.get("is_submittable")):
        raise MutationFailure(422, "This DocType does not support submit or cancel.")
    return types


def _editable(field: dict, doctype: str, *, creating: bool) -> bool:
    name = field.get("fieldname", "")
    kind = field.get("fieldtype")
    return (isinstance(name, str) and FIELD_NAME.fullmatch(name) is not None
            and not name.startswith("_") and name not in _RESERVED
            and not current_policy().blocked(name, doctype)
            and kind in _SUPPORTED and (creating or kind not in {"Table", "Table MultiSelect"})
            and field.get("permlevel") in (None, "", 0, "0")
            and not any(_flag(field.get(key)) for key in ("hidden", "read_only", "is_virtual")))


def describe_mutation_fields(doctype: str, operation: Operation, types: dict[str, dict]) -> list[dict]:
    if operation not in ("create", "update"):
        return []

    def describe(name: str, include_children: bool) -> list[dict]:
        fields = []
        for spec in types.get(name, {}).get("fields", []):
            if not _editable(spec, name, creating=operation == "create"):
                continue
            kind = spec["fieldtype"]
            item = {"fieldname": spec["fieldname"], "label": spec.get("label"),
                    "type": kind, "required_without_default": _flag(spec.get("reqd"))
                    and _empty(spec.get("default"))}
            options = spec.get("options") or ""
            if kind in {"Select", "Link"}:
                item["options"] = options[:800]
            if kind in {"Table", "Table MultiSelect"}:
                if not include_children or not _flag(types.get(options, {}).get("istable")):
                    continue
                item["child_fields"] = describe(options, False)
            fields.append(item)
        return sorted(fields, key=lambda item: not item["required_without_default"])[:40]

    return describe(doctype, True)


def _value(spec: dict, value, client: FrappeClient):
    kind, name = spec["fieldtype"], spec["fieldname"]
    if value is None or (isinstance(value, str) and not value.strip()):
        if _flag(spec.get("reqd")):
            raise MutationFailure(422, f"{name} requires a value.")
        return "" if kind in _TEXT else None
    if kind in _TEXT and isinstance(value, str) and len(value) <= 2000:
        if kind == "Select" and value not in (spec.get("options") or "").splitlines():
            raise MutationFailure(422, f"Choose an available option for {name}.")
        if kind == "Link":
            target = spec.get("options")
            if not target or not any(row.get("name") == value for row in client.get_list(
                target, ["name"], filters={"name": value}, limit=1,
            )):
                raise MutationFailure(422, f"Choose an existing link you can access for {name}.")
        return escape(value) if kind == "Text Editor" else value
    if kind in _TEMPORAL and isinstance(value, str):
        try:
            return _TEMPORAL[kind].fromisoformat(value).isoformat()
        except ValueError:
            pass
    if kind == "Check" and type(value) in (bool, int) and value in (0, 1):
        return int(value)
    if kind in {"Int", "Long Int"} and type(value) is int:
        return value
    if kind in _NUMBER - {"Check", "Int", "Long Int"} and type(value) in (int, float):
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    raise MutationFailure(422, f"Enter a valid {kind} value for {name}.")


def _payload(client: FrappeClient, doctype: str, raw: dict, types: dict[str, dict], *,
             creating: bool, children: bool = True) -> dict:
    specs = {row["fieldname"]: row for row in types[doctype].get("fields", []) if row.get("fieldname")}
    allowed = {name: row for name, row in specs.items() if _editable(row, doctype, creating=creating)}
    if not raw.keys() <= allowed.keys():
        raise MutationFailure(422, "The change includes an unavailable or unsupported field.")
    if creating:
        missing = [name for name, spec in allowed.items()
                   if _flag(spec.get("reqd")) and name not in raw and _empty(spec.get("default"))]
        if missing:
            raise MutationFailure(422, "Provide these required fields: " + ", ".join(missing) + ".")
    result = {}
    for name, value in raw.items():
        spec = allowed[name]
        if spec["fieldtype"] not in {"Table", "Table MultiSelect"}:
            result[name] = _value(spec, value, client)
            continue
        target = spec.get("options")
        if (not children or not _flag(types.get(target, {}).get("istable"))
                or not isinstance(value, list) or len(value) > _MAX_TABLE_ROWS
                or any(not isinstance(row, dict) for row in value)):
            raise MutationFailure(422, f"Provide up to {_MAX_TABLE_ROWS} valid child rows for {name}.")
        if not value and _flag(spec.get("reqd")):
            raise MutationFailure(422, f"{name} requires at least one row.")
        result[name] = [_payload(client, target, row, types, creating=True, children=False) for row in value]
    return result


def _existing(client: FrappeClient, user: UserContext, request: MutationRequest) -> dict:
    name = request.name
    if name is None or name in {".", ".."} or "/" in name or "\\" in name:
        raise MutationFailure(422, "Provide the exact Frappe record ID.")
    _permission(client, request.doctype, _PERMISSION[request.operation], name)
    if request.operation in ("submit", "cancel"):
        _permission(client, request.doctype, "write", name)
    doc = client.get_doc(request.doctype, name)
    if doc.get("name") != name or not doc.get("modified"):
        raise MutationFailure(404, "Frappe did not return that record and its current version.")
    if any(doc.get(field) != value for field, _, value in self_filters(request.doctype, user)):
        raise MutationFailure(403, "This chat tool is limited to your own record.")
    status = doc.get("docstatus")
    if request.operation in ("update", "submit") and status not in (0, "0"):
        raise MutationFailure(422, "Choose a draft record for this operation.")
    if request.operation == "cancel" and status not in (1, "1"):
        raise MutationFailure(422, "Choose a submitted record to cancel.")
    if request.operation == "delete" and status in (1, "1"):
        raise MutationFailure(422, "Cancel the submitted record before requesting deletion.")
    return doc


def prepare_mutation(client: FrappeClient, user: UserContext, request: MutationRequest,
                     types: dict[str, dict] | None = None) -> PreparedMutation:
    if types is None:
        types = load_mutation_schema(client, request.doctype, request.operation)
    if request.operation == "create":
        if request.name is not None:
            raise MutationFailure(422, "Frappe assigns the ID of the new record.")
        doc = {}
    else:
        doc = _existing(client, user, request)
    if request.operation not in ("create", "update") and request.payload:
        raise MutationFailure(422, "This operation only needs a record ID.")
    raw = dict(request.payload)
    if request.operation in ("create", "update"):
        for field, _, owner in self_filters(request.doctype, user):
            if field in raw and raw[field] != owner:
                raise MutationFailure(403, "This chat tool is limited to your own record.")
            if request.operation == "create" and field != "name":
                raw[field] = owner
            elif request.operation == "create":
                raise MutationFailure(403, "This chat tool can create employee-linked records only.")
        if request.operation == "update" and not raw:
            raise MutationFailure(422, "Specify at least one field to update.")
        payload = _payload(client, request.doctype, raw, types, creating=request.operation == "create")
    else:
        payload = {}
    if request.operation in ("submit", "cancel"):
        payload["docstatus"] = 1 if request.operation == "submit" else 2
    changes = tuple({"field": field, "before": doc.get(field), "after": value}
                    for field, value in payload.items())
    return PreparedMutation(request, payload, doc.get("modified"), changes)


def frappe_failure(exc: FrappeError, *, dispatched: bool = False) -> MutationFailure:
    if exc.error_type == "TimestampMismatchError" or exc.status_code == 409:
        return MutationFailure(409, "The record changed after review. Request a new proposal.")
    if exc.error_type == "CSRFTokenError":
        return MutationFailure(403, "Refresh Frappe Desk and request a new proposal.")
    if exc.status_code in (401, 403) or exc.error_type == "PermissionError":
        return MutationFailure(403, "Frappe denied permission for this change.")
    if exc.error_type == "MandatoryError":
        return MutationFailure(422, "Frappe requires additional fields. Check the mandatory fields in Desk.")
    if exc.status_code == 404:
        return MutationFailure(404, "Frappe could not find the record or linked record.")
    if exc.status_code in (400, 417, 422) or exc.error_type == "ValidationError":
        return MutationFailure(422, "Frappe rejected the change. Check the field values and document state in Desk.")
    if dispatched:
        return MutationFailure(502, "The write outcome could not be verified. Check the record in Frappe before retrying.")
    return MutationFailure(503, "Frappe could not verify the proposed change.")
