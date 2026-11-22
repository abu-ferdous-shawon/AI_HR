"""Typed universal tools that require the current user's verified retrieval context."""
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.retriever import ReadContext, require_read_context, schema_view, self_filters
from app.auth.security import AccessDenied, ToolInputError, UserContext
from app.core.masking import is_confidential
from app.frappe_client.client import ORDER_FIELD
from app.indexer.alias_resolver import normalize
from app.knowledge_packs.redactor import current_policy
from app.tools.registry import tool

READ_TOOLS = frozenset({"search_schema", "query_records", "get_record_detail", "aggregate_records", "run_frappe_report"})
MAX_ROWS = 50
_NUMERIC = {"Int", "Long Int", "Float", "Currency", "Percent", "Duration", "Rating", "Check"}
Text = Annotated[str, StringConstraints(min_length=1, max_length=140, strip_whitespace=True)]
Column = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=64)]
Scalar = Text | int | float | bool
FilterValue = Scalar | Annotated[list[Scalar], Field(min_length=1, max_length=20)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class Search(Arguments):
    query: Annotated[str, StringConstraints(min_length=1, max_length=180)]


class Filter(Arguments):
    field: Column
    op: Literal["=", "!=", "<", "<=", ">", ">=", "like", "not like", "in", "not in", "between", "is"]
    value: FilterValue


class RecordQuery(Arguments):
    doctype: Text
    fields: list[Column] = Field(min_length=1, max_length=18)
    filters: list[Filter] = Field(default_factory=list, max_length=12)
    or_filters: list[Filter] = Field(default_factory=list, max_length=8)
    order_by: Annotated[str, StringConstraints(min_length=1, max_length=95)] | None = None
    limit: int = Field(default=20, ge=1, le=MAX_ROWS)
    start: int = Field(default=0, ge=0, le=500)


class Detail(Arguments):
    doctype: Text
    name: Text


class Aggregate(Arguments):
    doctype: Text
    group_by: Column | None = None
    aggregate_fn: Literal["count", "sum", "avg", "min", "max"]
    aggregate_field: Column = "name"
    filters: list[Filter] = Field(default_factory=list, max_length=12)


class Report(Arguments):
    report_name: Text
    filters: dict[Column, FilterValue] = Field(default_factory=dict, max_length=16)


def _schema(context: ReadContext, doctype: str, *, listable: bool = True) -> dict:
    schema = context.schemas.get(doctype)
    if schema is None:
        raise ToolInputError("Choose a DocType exactly as shown in the verified schema.")
    if listable and schema["issingle"]:
        raise ToolInputError("This DocType is a single document. Use get_record_detail with its name.")
    return schema


def _fields(schema: dict, names) -> dict:
    allowed = {column["fieldname"]: column for column in schema["fields"]}
    if any(name not in allowed or current_policy().blocked(name, schema["name"]) for name in names):
        raise ToolInputError("Choose only fields listed in the verified schema.")
    return allowed


def _filters(schema: dict, filters: list[Filter]) -> list[list]:
    _fields(schema, (condition.field for condition in filters))
    prepared = []
    for condition in filters:
        value, op = condition.value, condition.op
        if isinstance(value, list) != (op in {"in", "not in", "between"}):
            raise ToolInputError("Use lists only with in, not in, or between.")
        if (op == "between" and len(value) != 2
                or op == "is" and value not in ("set", "not set")
                or op in {"like", "not like"} and not isinstance(value, str)):
            raise ToolInputError("Use a value matching the field and operator.")
        prepared.append([condition.field, op, value])
    return prepared


def _order(schema: dict, order: str | None) -> str | None:
    if order is None:
        return "name asc" if any(field["fieldname"] == "name" for field in schema["fields"]) else None
    columns = [ORDER_FIELD.fullmatch(term.strip()) for term in order.split(",")]
    if not all(columns):
        raise ToolInputError("Sort using field names and asc or desc.")
    _fields(schema, (column[1] for column in columns))
    return order


@tool("search_schema", "Show available verified DocType fields and status options for the topic.",
      input_model=Search)
def search_schema(ctx: UserContext, args: Search) -> dict:
    context = require_read_context(ctx)
    query = normalize(args.query)
    schemas = [schema_view(schema) for name, schema in context.schemas.items()
               if normalize(name) == query] or [schema_view(schema) for schema in context.schemas.values()]
    return {"schemas": schemas}


@tool("query_records", "List records the signed-in user can read. Use verified field names, typed filters, and pagination.",
      input_model=RecordQuery, allow=("*",))
def query_records(ctx: UserContext, args: RecordQuery) -> dict:
    context = require_read_context(ctx)
    schema = _schema(context, args.doctype)
    _fields(schema, args.fields)
    guard = self_filters(args.doctype, ctx)
    filters = guard + _filters(schema, args.filters)
    or_filters = _filters(schema, args.or_filters)
    projection = list(dict.fromkeys([*args.fields, *(field for field, _, _ in guard)]))
    rows = context.client.get_list(
        args.doctype, projection, filters=filters, or_filters=or_filters,
        order_by=_order(schema, args.order_by), limit=args.limit + 1, start=args.start,
    )
    if any(row.get(field) != value for field, _, value in guard for row in rows):
        raise AccessDenied("Frappe did not return a verifiable employee scope.")
    projected = [{key: row[key] for key in args.fields if key in row} for row in rows[:args.limit]]
    more = len(rows) > args.limit
    next_start = args.start + args.limit
    return {"doctype": args.doctype, "records": projected, "count": len(projected),
            "next_start": next_start if more and next_start <= 500 else None, "truncated": more}


@tool("get_record_detail", "Read a verified DocType record by its exact ID, including permitted child rows.",
      input_model=Detail, allow=("*",))
def get_record_detail(ctx: UserContext, args: Detail) -> dict:
    context = require_read_context(ctx)
    schema = _schema(context, args.doctype, listable=False)
    if args.name in {".", ".."} or (schema["issingle"] and args.name != args.doctype):
        raise ToolInputError("Use the exact record ID or the single DocType name.")
    guard = self_filters(args.doctype, ctx)
    if guard and guard[0][0] == "name" and args.name != ctx.employee_id:
        raise AccessDenied("This chat tool is limited to your own record.")
    if guard and guard[0][0] != "name":
        permitted = context.client.get_list(
            args.doctype, ["name"], filters=[*guard, ["name", "=", args.name]], limit=1,
        )
        if not permitted:
            raise AccessDenied("This chat tool is limited to your own record.")
    doc = context.client.get_doc(args.doctype, args.name)
    if not doc:
        raise ToolInputError("Frappe returned no record for that ID.")
    if any(doc.get(field) != value for field, _, value in guard):
        raise AccessDenied("This chat tool is limited to your own record.")
    allowed = {field["fieldname"] for field in schema["fields"]}
    result = {key: value for key, value in doc.items() if key in allowed}
    if schema["child_tables"]:
        for table, child_type in schema["child_tables"].items():
            if isinstance(doc.get(table), list) and not current_policy().blocked(table, args.doctype):
                # Full-document permissions were applied by Frappe to the child rows too.
                result[table] = [current_policy().sanitize(row, child_type)
                                 for row in doc[table][:MAX_ROWS] if isinstance(row, dict)]
    return {"doctype": args.doctype, "record": result}


@tool("aggregate_records", "Count or aggregate permitted records using Frappe. Supported functions are count, sum, avg, min and max.",
      input_model=Aggregate, allow=("*",))
def aggregate_records(ctx: UserContext, args: Aggregate) -> dict:
    context = require_read_context(ctx)
    schema = _schema(context, args.doctype)
    names = [args.aggregate_field] + ([args.group_by] if args.group_by else [])
    fields = _fields(schema, names)
    if args.aggregate_fn in {"sum", "avg"} and fields[args.aggregate_field]["fieldtype"] not in _NUMERIC:
        raise ToolInputError("Sum and average require a numeric field.")
    rows = context.client.get_aggregate(
        args.doctype, args.aggregate_fn, args.aggregate_field, group_by=args.group_by,
        filters=self_filters(args.doctype, ctx) + _filters(schema, args.filters), limit=MAX_ROWS + 1,
    )
    # Keep sensitive aggregates under the source field name so the masking vault handles them.
    key = "count" if args.aggregate_fn == "count" else args.aggregate_field
    if args.group_by == key:
        key = "amount" if is_confidential(args.aggregate_field) else "value"
    if key == args.group_by:
        key = "aggregate_value"
    grouped = []
    for row in rows[:MAX_ROWS]:
        if "_metric" not in row:
            raise ToolInputError("Frappe did not return the requested aggregate.")
        item = {key: row["_metric"]}
        if args.group_by:
            item[args.group_by] = row.get(args.group_by)
        grouped.append(item)
    return {"doctype": args.doctype, "function": args.aggregate_fn, "groups": grouped,
            "truncated": len(rows) > MAX_ROWS}


@tool("run_frappe_report", "Execute an available indexed Frappe Script Report or Query Report with its declared filters.",
      input_model=Report, allow=("*",))
def run_frappe_report(ctx: UserContext, args: Report) -> dict:
    context = require_read_context(ctx)
    report = context.reports.get(args.report_name)
    if report is None:
        raise ToolInputError("Choose a report exactly as shown in the verified reports.")
    fields = {field["fieldname"]: field for field in report["filters"]}
    if any(key not in fields or current_policy().blocked(key) for key in args.filters):
        raise ToolInputError("Use only the available report filters.")
    filters = dict(args.filters)
    ownership = self_filters(report["ref_doctype"], ctx)
    if ownership:
        if "employee" not in fields:
            raise AccessDenied("This report does not support the required employee scope.")
        filters["employee"] = ctx.employee_id
    if any(field["reqd"] and (field["fieldname"] not in filters or filters[field["fieldname"]] in (None, ""))
           for field in fields.values()):
        raise ToolInputError("Fill in the required report filters shown in the report definition.")
    output = context.client.call_method("frappe.desk.query_report.run", {
        "report_name": args.report_name, "filters": json.dumps(filters),
        "ignore_prepared_report": 1, "are_default_filters": 0,
    })
    if not isinstance(output, dict) or not isinstance(output.get("result"), list):
        raise ToolInputError("Frappe did not return a readable report result.")
    rows = output["result"]
    if ownership and any(not isinstance(row, dict) or row.get("employee") != ctx.employee_id for row in rows):
        raise AccessDenied("Frappe did not return a verifiable employee scope for this report.")
    # Give the masking vault named cells. Positional rows need trusted labels first.
    raw_columns = output.get("columns") or []
    columns = [column if isinstance(column, str) else column.get("fieldname", "") for column in raw_columns]
    columns = [normalize(column.split(":", 1)[0]).replace(" ", "_") for column in columns]
    reported = [{key: value for key, value in zip(columns, row) if key} if isinstance(row, list) else row
                for row in rows[:MAX_ROWS] if isinstance(row, (list, dict))]
    return {"report_name": args.report_name, "records": reported, "count": len(reported),
            "truncated": len(rows) > MAX_ROWS}
