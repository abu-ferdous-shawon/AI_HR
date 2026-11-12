from app.core.masking import register_confidential
from app.core.security import UserContext
from app.records import fetch_records, summarize_records
from app.registry import get_profile
from app.tools.registry import tool

_PROFILE = get_profile()                       # an invalid profile stops startup here
register_confidential(_PROFILE.confidential_field_names())

_TYPES = list(_PROFILE.enabled_types())
_CATALOG = "\n".join(f"- {key}: {rt.label}" for key, rt in _PROFILE.enabled_types().items())
_TOTALS = "\n".join(
    f"- {key}: {', '.join(n for n in rt.aggregations if n != 'count')}"
    for key, rt in _PROFILE.enabled_types().items() if len(rt.aggregations) > 1)

_SCOPE = {"type": "string", "enum": ["self", "team", "company"],
          "description": "self (default) = the person's own records; team = their direct "
                         "reports; company = everyone (HR only)."}
_FROM = {"type": "string", "description": "Start date, YYYY-MM-DD."}
_TO = {"type": "string", "description": "End date, YYYY-MM-DD."}
_STATUS = {"type": "string", "description": "Only records in this status, for example Approved."}
_TYPE = {"type": "string", "enum": _TYPES, "description": "Which kind of record."}


@tool(
    name="get_employee_records",
    description=(
        "List HR records of one type: the logged-in person's own by default, their direct "
        "reports with scope 'team', or everyone with scope 'company' (HR only). Name one "
        "employee in 'employee' when asked about a specific person. Dates are YYYY-MM-DD; "
        "for records that span a period (leave, shifts) any overlap counts. The outcome is "
        "'records' or 'no_records' and filters_used always says what was applied. If "
        "truncated is true there are more records: narrow the dates or status. Never guess "
        "when the outcome is no_records. Record types:\n" + _CATALOG),
    parameters={"type": "object", "properties": {
        "record_type": _TYPE, "from_date": _FROM, "to_date": _TO, "status": _STATUS,
        "scope": _SCOPE,
        "employee": {"type": "string",
                     "description": "Name or ID of one employee (scope team or company)."},
        "limit": {"type": "integer", "description": "How many records to return."},
    }, "required": ["record_type"]},
    allow=("*",),    # confidential values are masked by the agent, not removed here
)
def get_employee_records(ctx: UserContext, record_type=None, from_date=None, to_date=None,
                         status=None, scope=None, employee=None, limit=None) -> dict:
    return fetch_records(ctx, record_type, from_date=from_date, to_date=to_date,
                         status=status, scope=scope, employee=employee, limit=limit)


@tool(
    name="summarize_employee_records",
    description=(
        "Count or total HR records exactly (the server calculates; never add numbers "
        "yourself). aggregation defaults to count; named totals per record type are listed "
        "below, and an invalid name returns the valid ones. group_by splits the figure by a "
        "field such as status or department; an invalid name returns the valid ones. The "
        "answer states the source, period and statuses included. Money totals come back as "
        "placeholders. Totals by type:\n" + _TOTALS),
    parameters={"type": "object", "properties": {
        "record_type": _TYPE,
        "aggregation": {"type": "string", "description": "count, or a named total."},
        "from_date": _FROM, "to_date": _TO, "status": _STATUS, "scope": _SCOPE,
        "group_by": {"type": "string", "description": "Optional field to group by."},
    }, "required": ["record_type"]},
    allow=("*",),
)
def summarize_employee_records(ctx: UserContext, record_type=None, aggregation=None,
                               from_date=None, to_date=None, status=None, scope=None,
                               group_by=None) -> dict:
    return summarize_records(ctx, record_type, aggregation=aggregation, from_date=from_date,
                             to_date=to_date, status=status, scope=scope, group_by=group_by)