"""Enforce hard field exclusions before any result can be displayed or masked."""
import re
from functools import lru_cache

from app.config import settings
from app.core.sites import current_site
from app.knowledge_packs.loader import KnowledgePack, _configured_packs

SECRET_FIELDS = frozenset({
    "password", "new_password", "api_key", "api_secret", "auth_token",
    "access_token", "refresh_token", "client_secret", "user_secret",
})
NEVER_SEND_SCHEMA_VERSION = 1
FIELD_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def _field_name(value: str) -> str:
    candidate = value.strip().casefold()
    if FIELD_IDENTIFIER.fullmatch(candidate):
        return candidate
    return re.sub(r"[^a-z0-9_]+", "_", candidate).strip("_")


class NeverSendPolicy:
    def __init__(self, packs: tuple[KnowledgePack, ...] = ()):
        shared = set(SECRET_FIELDS)
        by_doctype: dict[str, set[str]] = {}
        for pack in packs:
            shared.update(_field_name(field) for field in pack.never_send.global_fields)
            for doctype, fields in pack.never_send.doctype_fields.items():
                by_doctype.setdefault(doctype.casefold(), set()).update(_field_name(field) for field in fields)
        self.global_fields = frozenset(shared)
        self.by_doctype = {name: frozenset(fields | shared) for name, fields in by_doctype.items()}
        # Outbound data with no DocType provenance uses the union to fail closed.
        self.unknown_fields = self.global_fields.union(*self.by_doctype.values())

    def fields_for(self, doctype: str | None = None) -> frozenset[str]:
        if doctype is None:
            return self.unknown_fields
        return self.by_doctype.get(doctype.casefold(), self.global_fields)

    def blocked(self, field: str, doctype: str | None = None) -> bool:
        return _field_name(field) in self.fields_for(doctype)

    def project(self, fields: list[str], doctype: str) -> list[str]:
        return [field for field in fields if not self.blocked(field, doctype)]

    def sanitize(self, value, doctype: str | None = None, *, document: bool = False):
        """Remove blocked fields. Document reads may trust Frappe's child DocType markers."""
        return self._sanitize(value, doctype, document=document)

    def _sanitize(self, value, doctype: str | None, *, document: bool):
        if isinstance(value, dict):
            denied = self.fields_for(doctype)
            output = {}
            for field, item in value.items():
                if isinstance(field, str) and _field_name(field) in denied:
                    continue
                if document and isinstance(item, list):
                    output[field] = self._document_rows(item, doctype, field)
                else:
                    output[field] = self._sanitize(item, None, document=False)
            if isinstance(value.get("columns"), list) and any(
                isinstance(value.get(key), list) for key in ("result", "rows", "data")
            ):
                self._redact_columns(value["columns"], output, doctype)
            return output
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item, doctype, document=document) for item in value]
        return value

    def _document_rows(self, rows, parent, field) -> list:
        output = []
        for row in rows:
            child = None
            if (isinstance(row, dict) and row.get("parenttype") == parent
                    and row.get("parentfield") == field and isinstance(row.get("doctype"), str)):
                child = row["doctype"]
            output.append(self._sanitize(row, child, document=child is not None))
        return output

    @staticmethod
    def _column_names(column) -> set[str]:
        if isinstance(column, str):
            return {_field_name(column.split(":", 1)[0])}
        if isinstance(column, dict):
            return {_field_name(column[key]) for key in ("fieldname", "label")
                    if isinstance(column.get(key), str)}
        return set()

    def _redact_columns(self, columns: list, output: dict, doctype: str | None) -> None:
        names = [self._column_names(column) for column in columns]
        denied = self.fields_for(doctype)
        blocked = {i for i, fields in enumerate(names) if fields & denied}
        if not blocked:
            return
        if "columns" in output:
            output["columns"] = [column for i, column in enumerate(output["columns"]) if i not in blocked]
        aliases = set().union(*(names[i] for i in blocked))
        for key in ("result", "rows", "data"):
            rows = output.get(key)
            if not isinstance(rows, list):
                continue
            for position, row in enumerate(rows):
                if isinstance(row, list):
                    rows[position] = [cell for i, cell in enumerate(row) if i not in blocked]
                elif isinstance(row, dict):
                    rows[position] = {k: v for k, v in row.items()
                                      if not isinstance(k, str) or _field_name(k) not in aliases}
        # A chart, summary or custom HTML can contain values from the omitted columns.
        for key in ("chart", "report_summary", "message"):
            output.pop(key, None)

    def sanitize_report(self, value, doctype: str | None = None):
        output = self.sanitize(value, doctype)
        if isinstance(output, dict):
            # Report add-ons can contain derived values outside the declared rows and columns.
            for key in ("chart", "report_summary", "message", "html", "html_format"):
                output.pop(key, None)
        return output


@lru_cache
def _policy(site_name: str, names: tuple[str, ...], directory: str) -> NeverSendPolicy:
    return NeverSendPolicy(_configured_packs(site_name, names, directory))


def current_policy() -> NeverSendPolicy:
    site = current_site()
    return _policy(site.name, tuple(site.knowledge_packs), settings.knowledge_pack_dir)
