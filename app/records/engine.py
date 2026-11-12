import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.frappe_client import FrappeError, get_frappe_client
from app.core.security import AccessDenied, ToolInputError, UserContext
from app.records.scope import Access, resolve_access
from app.registry import Profile, RecordType, get_profile
from app.registry.models import norm
from app.tools.common import parse_date, to_int

log = logging.getLogger("hr_ai.records")

DETAIL_MAX = 5            # child-table lines load only when this few records match
MAX_GROUPS = 50
MAX_LIMIT_REQUEST = 1_000_000
STATE_NAMES = {0: "draft", 1: "submitted", 2: "cancelled"}
CENT = Decimal("0.01")


@dataclass
class Query:
    filters: list
    used: dict             # what the person is told was applied
    period: dict | None
    status: str | None


# ---------- lookups and filters ----------

def _record_type(profile: Profile, text) -> tuple[str, RecordType]:
    available = ", ".join(profile.enabled_types())
    if text is None or not str(text).strip():
        raise ToolInputError(f"record_type is required. Available: {available}.")
    key = profile.resolve(str(text))
    if key is None:
        raise ToolInputError(f"Unknown record type '{str(text).strip()[:40]}'. "
                             f"Available: {available}.")
    return key, profile.get(key)


def _stamp(rt: RecordType, day, end_of_day: bool) -> str:
    if rt.date_is_datetime:
        return f"{day.isoformat()} {'23:59:59' if end_of_day else '00:00:00'}"
    return day.isoformat()


def _build_query(rt: RecordType, access: Access, from_date, to_date, status) -> Query:
    start = parse_date(from_date, "from_date") if from_date else None
    end = parse_date(to_date, "to_date") if to_date else None
    if start and end and start > end:
        raise ToolInputError("from_date must not be after to_date.")

    filters: list = []
    ignored: list[str] = []
    period = None

    column = ([rt.employee_via.child_doctype, rt.employee_via.child_field]
              if rt.employee_via else [rt.employee_field])
    if access.employee_ids is not None:
        ids = list(access.employee_ids)
        filters.append([*column, "=", ids[0]] if len(ids) == 1 else [*column, "in", ids])

    if start or end:
        if rt.date_field:
            if rt.date_end_field:       # records that overlap the period
                if end:
                    filters.append([rt.date_field, "<=", _stamp(rt, end, True)])
                if start:
                    filters.append([rt.date_end_field, ">=", _stamp(rt, start, False)])
            else:
                if start:
                    filters.append([rt.date_field, ">=", _stamp(rt, start, False)])
                if end:
                    filters.append([rt.date_field, "<=", _stamp(rt, end, True)])
            period = {"from_date": start.isoformat() if start else None,
                      "to_date": end.isoformat() if end else None,
                      "date_field": rt.date_field,
                      "match": "overlapping" if rt.date_end_field else "within"}
        else:
            ignored += [n for n, v in (("from_date", start), ("to_date", end)) if v]

    canon = None
    if status and str(status).strip():
        if not rt.status_field:
            ignored.append("status")
        else:
            canon = {norm(v): v for v in rt.status_values}.get(norm(status))
            if canon is None:
                raise ToolInputError(f"status must be one of: {', '.join(rt.status_values)}.")
            filters.append([rt.status_field, "=", canon])

    if rt.docstatus is not None:
        filters.append(["docstatus", "=", rt.docstatus[0]] if len(rt.docstatus) == 1
                       else ["docstatus", "in", list(rt.docstatus)])
    filters.extend([field, op, list(value) if isinstance(value, list) else value]
                   for field, op, value in rt.base_filters)

    used = {"scope": access.scope, "employee": access.employee, "period": period,
            "status": canon,
            "record_state": [STATE_NAMES[s] for s in rt.docstatus] if rt.docstatus else "any"}
    if ignored:
        used["ignored"] = ignored
    return Query(filters, used, period, canon)


def _fetch(rt: RecordType, filters: list, max_rows: int) -> list[dict]:
    try:
        return get_frappe_client().get_all(rt.doctype, ["*"], filters=filters,
                                           order_by=rt.order_by, max_rows=max_rows)
    except FrappeError as exc:
        if exc.status_code in (401, 403):      # the ERP refused: denied, not failed
            log.warning("The ERP refused access to %s: %s", rt.doctype, exc)
            raise AccessDenied(f"The HR system did not allow access to {rt.label.lower()}. "
                               "Please contact HR.") from exc
        raise                                   # anything else is a failure


def _unique(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        name = row.get("name")
        if name is not None and name in seen:
            continue
        seen.add(name)
        out.append(row)
    return out


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def _filled(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _header(key: str, rt: RecordType, access: Access, query: Query) -> dict:
    out = {"record_type": key, "label": rt.label, "source_doctype": rt.doctype,
           "scope": access.scope, "period": query.period, "filters_used": query.used}
    if access.employee:
        out["employee"] = access.employee
    return out


# ---------- listing ----------

def _project(rt: RecordType, row: dict) -> dict:
    out = {"name": row["name"]} if _filled(row.get("name")) else {}
    for field in rt.fields:
        if field != "name" and _filled(row.get(field)):
            out[field] = _clean(row[field])
    return out


def _line_items(rt: RecordType, doc: dict, allowed: set | None) -> tuple[list, list]:
    items, truncated = [], []
    for table_key, table in rt.child_tables.items():
        rows = doc.get(table_key) or []
        if rt.employee_via and table_key == rt.employee_via.table_field and allowed is not None:
            rows = [r for r in rows if r.get(rt.employee_via.child_field) in allowed]
        if len(rows) > table.max_rows:
            truncated.append(table_key)
        for row in rows[:table.max_rows]:
            item = {"section": table_key}
            item.update({f: _clean(row[f]) for f in table.fields if _filled(row.get(f))})
            items.append(item)
    return items, truncated


def _attach_details(rt: RecordType, records: list[dict], access: Access) -> bool:
    allowed = set(access.employee_ids) if access.employee_ids is not None else None
    client = get_frappe_client()
    try:
        for record in records:
            doc = client.get_doc(rt.doctype, record["name"])
            items, cut = _line_items(rt, doc, allowed)
            if items:
                record["line_items"] = items
            if cut:
                record["line_items_truncated"] = cut
    except FrappeError as exc:
        log.warning("Could not load details of %s: %s", rt.doctype, exc)
        for record in records:
            record.pop("line_items", None)
            record.pop("line_items_truncated", None)
        return False
    return True


def fetch_records(ctx: UserContext, record_type, *, from_date=None, to_date=None,
                  status=None, scope=None, employee=None, limit=None,
                  profile: Profile | None = None) -> dict:
    """Outcome 'records' or 'no_records'. Denial raises AccessDenied, failure FrappeError."""
    prof = profile or get_profile()
    key, rt = _record_type(prof, record_type)
    access = resolve_access(ctx, prof, rt, scope, employee)
    query = _build_query(rt, access, from_date, to_date, status)

    requested = to_int(limit, "limit", 1, MAX_LIMIT_REQUEST)
    applied = min(requested or rt.default_limit, rt.max_limit)
    rows = _unique(_fetch(rt, query.filters, applied + 1))      # one extra row shows "more exist"

    result = _header(key, rt, access, query)
    result["limit"] = applied
    if requested and requested > applied:
        result["limit_capped"] = True
    if not rows:
        return {**result, "outcome": "no_records",
                "message": f"No {rt.label.lower()} were found for these filters."}

    truncated = len(rows) > applied
    records = [_project(rt, row) for row in rows[:applied]]
    result.update(outcome="records", count=len(records), truncated=truncated, records=records)
    if rt.child_tables:
        result["details_included"] = (len(records) <= DETAIL_MAX
                                      and _attach_details(rt, records, access))
    return result


# ---------- totals (computed in Python with Decimal) ----------

def _numbers(rows: list[dict], field: str) -> tuple[list[Decimal], int]:
    values, skipped = [], 0
    for row in rows:
        raw = row.get(field)
        if not _filled(raw):
            continue
        try:
            number = Decimal(str(raw).strip())
        except InvalidOperation:
            skipped += 1
            continue
        if number.is_finite():
            values.append(number)
        else:
            skipped += 1
    return values, skipped


def _measure(agg, rows: list[dict]) -> tuple[float | int | None, int, int]:
    """(value, numeric rows used, rows skipped as non-numeric)"""
    if agg.op == "count":
        return len(rows), len(rows), 0
    values, skipped = _numbers(rows, agg.field)
    if not values:
        return (0.0 if agg.op == "sum" else None), 0, skipped
    total = sum(values, Decimal(0))
    result = {"sum": total, "avg": total / len(values),
              "min": min(values), "max": max(values)}[agg.op]
    return float(result.quantize(CENT, rounding=ROUND_HALF_UP)), len(values), skipped


def _groups(agg, rows: list[dict], field: str, figure: str) -> tuple[list[dict], bool]:
    buckets = defaultdict(list)
    for row in rows:
        raw = row.get(field)
        buckets[str(raw).strip() if _filled(raw) else "(none)"].append(row)
    groups = []
    for label, items in buckets.items():
        value, _, _ = _measure(agg, items)
        groups.append({"group": label, "records": len(items), figure: value})
    groups.sort(key=lambda g: (g[figure] is None, -(g[figure] or 0), g["group"]))
    return groups[:MAX_GROUPS], len(groups) > MAX_GROUPS


def _choose(options: dict, text, what: str, default=None):
    if text is None or not str(text).strip():
        return default
    found = {norm(k): k for k in options}.get(norm(text))
    if found is None:
        raise ToolInputError(f"{what} must be one of: {', '.join(options)}.")
    return found


def summarize_records(ctx: UserContext, record_type, *, aggregation=None, from_date=None,
                      to_date=None, status=None, scope=None, group_by=None,
                      profile: Profile | None = None) -> dict:
    prof = profile or get_profile()
    key, rt = _record_type(prof, record_type)
    access = resolve_access(ctx, prof, rt, scope, None)
    query = _build_query(rt, access, from_date, to_date, status)

    name = _choose(rt.aggregations, aggregation, "aggregation", default="count")
    agg = rt.aggregations[name]
    group_field = None
    if group_by is not None and str(group_by).strip():
        if not rt.group_by_allowed:
            raise ToolInputError(f"{rt.label} cannot be grouped.")
        group_field = _choose(dict.fromkeys(rt.group_by_allowed), group_by, "group_by")

    cap = prof.limits.max_rows
    rows = _unique(_fetch(rt, query.filters, cap + 1))
    if len(rows) > cap:
        raise ToolInputError(f"There are too many {rt.label.lower()} to total ({cap} is the "
                             "limit). Please choose a shorter period.")

    result = _header(key, rt, access, query)
    result.update(aggregation=name, aggregation_label=agg.label or name, operation=agg.op)
    if not rows:
        return {**result, "outcome": "no_records", "records_counted": 0,
                "message": f"No {rt.label.lower()} were found for these filters."}

    figure = "amount" if agg.money else "value"          # 'amount' is masked from the model
    value, used, skipped = _measure(agg, rows)
    result.update(outcome="records", records_counted=len(rows), **{figure: value})
    result["statuses_included"] = ([query.status] if query.status else
                                   sorted({str(r[rt.status_field]).strip() for r in rows
                                           if rt.status_field and _filled(r.get(rt.status_field))}))
    if agg.field:
        result["numeric_records"] = used
        if skipped:
            result["skipped_non_numeric"] = skipped
    if group_field:
        result["group_by"] = group_field
        result["groups"], more = _groups(agg, rows, group_field, figure)
        if more:
            result["groups_truncated"] = True
    return result