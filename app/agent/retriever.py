"""Select indexed metadata, then verify it as the signed-in Frappe user."""
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.auth.credentials import current_holder
from app.auth.security import AccessDenied, UserContext
from app.core.sites import SiteConfig
from app.frappe_client.client import FrappeClient, FrappeError, NON_COLUMN_TYPES
from app.frappe_client.schema_extractor import INTERNAL
from app.indexer.alias_resolver import normalize
from app.indexer.embeddings import EmbeddingError, create_embedder
from app.indexer.sync import open_snapshot
from app.indexer.vector_store import IndexError, read_json, read_tier, search_tier
from app.knowledge_packs.loader import KnowledgePack, VocabularyEntry, active_packs
from app.knowledge_packs.redactor import current_policy

_CANDIDATES = 4
_MAX_FIELDS = 18
_COMMON = ("status", "docstatus", "posting_date", "transaction_date", "attendance_date", "date", "company", "employee")
_REPORT_WORDS = re.compile(r"\b(report|register|sheet|analysis|summary|aging|ledger)\b", re.I)
# Preserve the existing self-only boundary even if the site grants broader read access.
_SELF_ONLY = {"Employee": "name", "Salary Slip": "employee", "Leave Application": "employee"}
_NON_QUERYABLE = INTERNAL | {"User", "Role", "Has Role", "Employee Education", "Salary Detail"}


@dataclass
class ReadContext:
    client: FrappeClient
    user: UserContext
    schemas: dict[str, dict] = field(default_factory=dict)
    reports: dict[str, dict] = field(default_factory=dict)
    denied_target: bool = False
    company: str | None = None
    guidance: list[dict] = field(default_factory=list)
    matched_doctypes: list[str] = field(default_factory=list)
    matched_reports: list[str] = field(default_factory=list)


_ACTIVE: ContextVar[ReadContext | None] = ContextVar("frappe_read_context", default=None)


@contextmanager
def use_read_context(context: ReadContext):
    token = _ACTIVE.set(context)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def require_read_context(user: UserContext) -> ReadContext:
    context = _ACTIVE.get()
    holder = current_holder()
    if context is None or holder is None or holder.client is not context.client or context.user != user:
        raise AccessDenied("This read requires your signed-in Frappe session and a verified schema.")
    return context


def self_filters(doctype: str, user: UserContext) -> list:
    if owner := _SELF_ONLY.get(doctype):
        return [[owner, "=", user.employee_id]]
    return []


def _matches(text: str, name: str) -> bool:
    return bool(name) and f" {name} " in f" {text} "


def _named_targets(text: str, aliases: dict[str, list[str]]) -> list[str]:
    # Very short aliases match only the entire query.
    matched = (alias for alias in sorted(aliases, key=lambda key: (-len(key), key))
               if alias == text or (len(alias) >= 3 and _matches(text, alias)))
    return list(dict.fromkeys(name for alias in matched for name in aliases[alias]))


def _pruned_fields(schema: dict, ranked: list[dict], packs: tuple[KnowledgePack, ...]) -> list[dict]:
    fields = {row["fieldname"]: row for row in schema["fields"] if row["fieldtype"] not in NON_COLUMN_TYPES}
    if schema.get("is_submittable"):
        # Frappe supplies docstatus as a standard column rather than a custom schema field.
        fields.setdefault("docstatus", {
            "fieldname": "docstatus", "label": "Document status", "fieldtype": "Int",
            "options": "0 = Draft\n1 = Submitted\n2 = Cancelled",
        })
    configured_fields = []
    for pack in packs:
        if details := pack.doctypes.get(schema["name"]):
            configured_fields.extend(n for n in (details.owner_link, details.status_field, *details.date_fields) if n)
    names = dict.fromkeys([*configured_fields, *_COMMON, *ranked, *fields])
    return [fields[n] for n in names if n in fields][:_MAX_FIELDS]


def _verify_schema(context: ReadContext, schema: dict, requested: list[dict]) -> dict:
    client, name = context.client, schema["name"]
    fields = [item["fieldname"] for item in requested]
    if schema.get("issingle"):
        allowed = set(client.get_doc(name, name, fields=["name", *fields]))
    else:
        rows = client.get_list(name, ["name", *fields], filters=self_filters(name, context.user), limit=1)
        # An empty query proves access to the list, but supplies no evidence for individual fields.
        allowed = set(rows[0]) if rows else {"name"}
    policy = current_policy()
    allowed = set(policy.project(list(allowed), name))
    visible = [item for item in requested if item["fieldname"] in allowed]
    if "name" in allowed and "name" not in fields:
        visible.insert(0, {"fieldname": "name", "label": "ID", "fieldtype": "Data"})
    return {
        "name": name, "module": schema["module"], "issingle": bool(schema.get("issingle")),
        "fields": visible,
        "status_options": schema["status_options"] if "status" in allowed else [],
        "workflow_states": [workflow for workflow in schema["workflow_states"]
                            if workflow["fieldname"] in allowed],
        "relationships": [relation for relation in schema["relationships"]
                          if relation["fieldname"] in allowed],
        # Only the table keys are kept internally. Child data is permission-checked during detail reads.
        "child_tables": {relation["fieldname"]: relation["target"] for relation in schema["relationships"]
                         if relation["kind"] in {"Table", "Table MultiSelect"}},
    }


def _guidance(context: ReadContext, packs: tuple[KnowledgePack, ...]) -> list[dict]:
    selected = []
    policy = current_policy()
    for pack in packs:
        for name, schema in context.schemas.items():
            definition = pack.doctypes.get(name)
            fields = {field["fieldname"] for field in schema["fields"]}
            if definition is None:
                continue
            protected = {normalize(field) for field in policy.fields_for(name)}

            def safe(text: str | None) -> bool:
                return bool(text) and not any(_matches(normalize(text), term) for term in protected)

            item = {
                "doctype": name,
                "date_fields": [field for field in definition.date_fields if field in fields],
                "examples": [{"question": example.question, "notes": example.notes}
                             for example in pack.examples if example.target_doctype == name
                             and safe(example.question) and safe(example.notes)][:2],
                "hints": [hint for hint in pack.master_hints if safe(hint)][:2],
                "kpis": {phrase: spec.description for phrase, spec in pack.kpis.items()
                         if isinstance(spec, VocabularyEntry) and spec.target_doctype == name
                         and safe(phrase) and safe(spec.description)},
            }
            if definition.status_field in fields:
                item["status_field"] = definition.status_field
                item["states"] = definition.states
            if definition.owner_link in fields:
                item["owner_link"] = definition.owner_link
            selected.append(item)
        for report in pack.recommended_reports:
            if report.name in context.reports and not any(
                _matches(normalize(report.when_to_use), normalize(field)) for field in policy.unknown_fields
            ):
                selected.append({"report": report.name, "when_to_use": report.when_to_use})
    return selected


def schema_view(schema: dict) -> dict:
    return {name: value for name, value in schema.items() if name != "child_tables"}


def retrieve(site: SiteConfig, client: FrappeClient, user: UserContext, query: str) -> ReadContext:
    context = ReadContext(client, user)
    packs = active_packs(site)
    try:
        with open_snapshot(site, packs) as (path, manifest):
            aliases = read_json(path / "alias_map.json")
            keywords = read_json(path / "business_keywords.json")
            table_cards, table_vectors = read_tier(path, "table_index")
            report_cards, report_vectors = read_tier(path, "report_index")
            field_cards, field_vectors = read_tier(path, "field_index")
    except (RuntimeError, OSError, ValueError):
        return context

    terms = normalize(query)
    explicit = [name for name in _named_targets(terms, aliases)
                if name not in _NON_QUERYABLE and name in manifest["schemas"]
                and not manifest["schemas"][name]["istable"]]
    report_exact = [card["key"] for card in report_cards if _matches(terms, normalize(card["key"]))]
    candidates = explicit[:_CANDIDATES]
    report_names = report_exact[:2]
    ranked_fields: dict[str, list[str]] = {}
    embedding = None
    try:
        embedding = create_embedder(site)
        vectors = list(embedding.encode([query[:1500]], query=True))
        if len(vectors) != 1:
            raise EmbeddingError("Expected one query embedding.")

        class QueryVector:
            dimension = embedding.dimension

            def encode(self, texts, *, query):
                return vectors

        prepared = QueryVector()
        if not candidates:
            suggestions = search_tier(table_cards, table_vectors, prepared, query, 10)
            matches = dict.fromkeys([
                *_named_targets(terms, keywords),
                *(hit["key"] for hit in suggestions if hit["score"] > 0.20),
            ])
            candidates = [name for name in matches if name not in _NON_QUERYABLE
                          and name in manifest["schemas"] and not manifest["schemas"][name]["istable"]][:_CANDIDATES]
        if _REPORT_WORDS.search(query) and not report_names:
            report_names = [hit["key"] for hit in search_tier(
                report_cards, report_vectors, prepared, query, 3,
            ) if hit["score"] > 0.32][:2]
        for name in candidates:
            ranked_fields[name] = [hit["data"]["fieldname"] for hit in search_tier(
                field_cards, field_vectors, prepared, query, _MAX_FIELDS, name,
            )]
    except (EmbeddingError, IndexError, ValueError):
        pass  # Exact aliases and report names still work during an embedding outage.
    finally:
        if embedding is not None and hasattr(embedding, "close"):
            embedding.close()

    context.matched_doctypes = candidates
    context.matched_reports = report_names
    for name in candidates:
        schema = manifest["schemas"].get(name)
        if not schema or schema["istable"]:
            continue
        try:
            context.schemas[name] = _verify_schema(
                context, schema, _pruned_fields(schema, ranked_fields.get(name, []), packs),
            )
        except FrappeError as exc:
            if (exc.status_code in (401, 403) or exc.error_type == "PermissionError") and name in explicit and not report_exact:
                context.denied_target = True
                return context

    for name in report_names:
        report = manifest["reports"].get(name)
        if not report or report["ref_doctype"] in _NON_QUERYABLE:
            continue
        try:
            permission = client.call_method("frappe.desk.query_report.get_script", {"report_name": name})
        except FrappeError as exc:
            if (exc.status_code in (401, 403) or exc.error_type == "PermissionError") and name in report_exact:
                context.denied_target = True
                return context
            continue
        if isinstance(permission, dict) and not permission.get("custom_report_name"):
            context.reports[name] = report

    if context.schemas or context.reports:
        try:
            rows = client.get_list("Employee", ["company"], filters={"name": user.employee_id}, limit=1)
            if rows and isinstance(rows[0].get("company"), str):
                context.company = rows[0]["company"]
        except FrappeError:
            pass
        context.guidance = _guidance(context, packs)
    return context


def render_context(context: ReadContext) -> str:
    return json.dumps({
        "schemas": [schema_view(schema) for schema in context.schemas.values()],
        "reports": [{"name": name, "filters": value["filters"]} for name, value in context.reports.items()],
        "knowledge": context.guidance,
    }, ensure_ascii=False)
