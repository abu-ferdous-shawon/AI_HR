import argparse
import difflib
import sys
from dataclasses import dataclass, field

from app.config import settings
from app.frappe_client.client import FrappeError, get_service_client
from app.registry.loader import RegistryError, load_profile
from app.registry.models import RecordType, norm

STANDARD_COLUMNS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}
STANDARD_CHILD = STANDARD_COLUMNS | {"parent", "parentfield", "parenttype"}
LAYOUT_TYPES = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold"}
TABLE_TYPES = {"Table", "Table MultiSelect"}
ROW_CAP = 200              # the probe never counts beyond this
MAX_STATUS_VALUES = 30
EXTRA_SHOWN = 40


# ---------- what the ERP looks like ----------

@dataclass
class TableInfo:
    doctype: str | None = None
    fields: set[str] | None = None          # None = not known (empty table and no field list)


@dataclass
class Structure:
    source: str = "none"                    # rows, meta, rows+meta or none
    fields: dict[str, str] = field(default_factory=dict)         # name -> field type ('' if unknown)
    options: dict[str, list[str]] = field(default_factory=dict)  # select field -> allowed values
    tables: dict[str, TableInfo] = field(default_factory=dict)   # child tables inside the record
    tables_known: bool = False              # True once we looked inside a record or its metadata


def structure_from_row(row: dict, doc: dict | None) -> Structure:
    """Field names from one sample record (names only; the values are never kept)."""
    found = Structure(source="rows", fields={key: "" for key in row})
    if doc is not None:
        found.tables_known = True
        for key, value in doc.items():
            if isinstance(value, list):
                names = {k for item in value if isinstance(item, dict) for k in item}
                found.tables[key] = TableInfo(None, names or None)
                found.fields.setdefault(key, "Table")
    return found


def structure_from_meta(docs: list[dict], doctype: str) -> Structure:
    """Field names from the ERP's own definition of the doctype and its child tables."""
    if not docs:
        return Structure()
    main = next((d for d in docs if d.get("name") == doctype), docs[0])
    children = {d.get("name"): d for d in docs if d is not main}
    found = Structure(source="meta", tables_known=True)
    for item in main.get("fields") or []:
        name, kind = item.get("fieldname"), item.get("fieldtype") or ""
        if not name or kind in LAYOUT_TYPES:
            continue
        found.fields[name] = kind
        if kind == "Select" and item.get("options"):
            found.options[name] = [o.strip() for o in str(item["options"]).split("\n") if o.strip()]
        if kind in TABLE_TYPES:
            child = children.get(item.get("options"))
            names = None
            if child is not None:
                names = {f.get("fieldname") for f in child.get("fields") or []
                         if f.get("fieldname") and f.get("fieldtype") not in LAYOUT_TYPES}
            found.tables[name] = TableInfo(item.get("options"), names)
    for column in STANDARD_COLUMNS:
        found.fields.setdefault(column, "")
    return found


def merge(a: Structure, b: Structure) -> Structure:
    if a.source == "none":
        return b
    if b.source == "none":
        return a
    out = Structure(source="rows+meta", tables_known=a.tables_known or b.tables_known)
    out.fields = {**a.fields, **{k: v or a.fields.get(k, "") for k, v in b.fields.items()}}
    out.options = {**a.options, **b.options}
    out.tables = dict(a.tables)
    for name, info in b.tables.items():
        old = out.tables.get(name)
        if old is None:
            out.tables[name] = info
        else:
            names = (info.fields or set()) | (old.fields or set())
            out.tables[name] = TableInfo(info.doctype or old.doctype, names or None)
    return out


@dataclass
class Observed:
    access: str = "ok"                      # ok, denied, missing or failed
    rows: int = 0                           # records seen (-1 = could not read)
    structure: Structure = field(default_factory=Structure)
    statuses: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _label(exc: FrappeError) -> str:
    if exc.status_code in (401, 403):
        return "denied"
    return "missing" if exc.status_code == 404 else "failed"


def observe(client, rt: RecordType) -> Observed:
    """Look at one doctype on the ERP: how many records, which fields, which statuses."""
    try:
        names = client.get_all(rt.doctype, ["name"], max_rows=ROW_CAP + 1)
    except FrappeError as exc:
        return Observed(access=_label(exc), rows=-1, notes=[f"the ERP answered {exc.status_code}"])

    seen = Observed(rows=len(names))
    from_rows = Structure()
    if names:
        try:
            sample = client.get_list(rt.doctype, ["*"], order_by="modified desc", limit=1)
        except FrappeError as exc:
            sample = []
            seen.notes.append(f"a sample record could not be read ({exc.status_code})")
        if sample:
            doc = None
            if rt.child_tables and sample[0].get("name"):
                try:
                    doc = client.get_doc(rt.doctype, sample[0]["name"])
                except FrappeError as exc:
                    seen.notes.append(f"the sample record could not be opened ({exc.status_code})")
            from_rows = structure_from_row(sample[0], doc)
        if rt.status_field:
            try:
                rows = client.get_all(rt.doctype, [rt.status_field], max_rows=ROW_CAP)
                values = {str(r[rt.status_field]).strip() for r in rows
                          if r.get(rt.status_field) not in (None, "")}
                if len(values) <= MAX_STATUS_VALUES:
                    seen.statuses = sorted(values)
            except FrappeError:
                pass                              # a wrong status field is reported by analyse()

    from_meta = Structure()
    try:
        from_meta = structure_from_meta(client.get_meta(rt.doctype), rt.doctype)
    except FrappeError as exc:
        seen.notes.append(f"the field list was not available ({exc.status_code})")
    if from_meta.source == "none":                # fall back to the DocType record itself
        try:
            fields = client.get_doc("DocType", rt.doctype).get("fields")
            if isinstance(fields, list) and fields:
                from_meta = structure_from_meta([{"name": rt.doctype, "fields": fields}],
                                                rt.doctype)
        except FrappeError:
            pass
    seen.structure = merge(from_rows, from_meta)
    return seen


# ---------- comparing with the profile ----------

@dataclass
class Gap:
    place: str
    name: str
    blocking: bool = False


@dataclass
class TypeReport:
    key: str
    doctype: str
    access: str
    rows: int
    source: str
    flagged_unverified: bool
    gaps: list[Gap] = field(default_factory=list)
    similar: dict[str, list[str]] = field(default_factory=dict)
    status_unknown: list[str] = field(default_factory=list)
    status_basis: str = ""
    extra: list[str] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.access != "ok":
            return "UNAVAILABLE"
        if self.source == "none":
            return "UNKNOWN"
        if any(g.blocking for g in self.gaps):
            return "BLOCKED"
        if self.gaps or self.status_unknown:
            return "FIX"
        return "OK"


def analyse(key: str, rt: RecordType, seen: Observed) -> TypeReport:
    s = seen.structure
    report = TypeReport(key, rt.doctype, seen.access, seen.rows, s.source, rt.verify,
                        notes=list(seen.notes))
    if seen.access != "ok" or s.source == "none":
        return report
    known = set(s.fields) | STANDARD_COLUMNS

    def check(place: str, names, blocking: bool = False, pool: set | None = None) -> None:
        pool = known if pool is None else pool
        for name in names:
            if name and name not in pool:
                report.gaps.append(Gap(place, name, blocking))
                close = difflib.get_close_matches(name, sorted(pool - STANDARD_COLUMNS),
                                                  n=3, cutoff=0.6)
                if close:
                    report.similar[name] = close

    check("fields", rt.fields)
    check("employee_field", [rt.employee_field], blocking=True)
    check("base_filters", [f for f, _, _ in rt.base_filters], blocking=True)
    check("date_field", [rt.date_field])
    check("date_end_field", [rt.date_end_field])
    check("status_field", [rt.status_field])

    if s.tables_known:
        if rt.employee_via:
            via = rt.employee_via
            info = s.tables.get(via.table_field)
            if info is None:
                report.gaps.append(Gap("employee_via", f"table {via.table_field}", True))
            else:
                if info.doctype and info.doctype != via.child_doctype:
                    report.gaps.append(Gap("employee_via", f"child doctype is {info.doctype}", True))
                if info.fields is not None and via.child_field not in info.fields | STANDARD_CHILD:
                    report.gaps.append(Gap("employee_via", f"field {via.child_field}", True))
        for table, ct in rt.child_tables.items():
            info = s.tables.get(table)
            if info is None:
                report.gaps.append(Gap("child_tables", table))
            elif info.fields is None:
                report.notes.append(f"the fields of table '{table}' could not be checked "
                                    "(it is empty and the ERP gave no field list)")
            else:
                check(f"table {table}", ct.fields, pool=info.fields | STANDARD_CHILD)

    if not s.tables_known and (rt.child_tables or rt.employee_via):
        report.notes.append("child tables could not be checked (no sample record could be "
                            "opened and the ERP gave no field list)")
        allowed = s.options.get(rt.status_field)
        values = allowed or seen.statuses
        report.status_basis = "allowed by the ERP" if allowed else "in use"
        have = {norm(v) for v in rt.status_values}
        report.status_unknown = sorted(v for v in values if norm(v) not in have)
    allowed = s.options.get(rt.status_field) if rt.status_field else None
    values = allowed or seen.statuses
    report.status_basis = "allowed by the ERP" if allowed else "in use"
    have = {norm(v) for v in rt.status_values}
    report.status_unknown = sorted(v for v in values if norm(v) not in have)
    used = set(rt.fields) | {f for f, _, _ in rt.base_filters}
    used |= {x for x in (rt.employee_field, rt.date_field, rt.date_end_field, rt.status_field) if x}
    report.extra = sorted(n for n in s.fields
                          if n not in used and n not in STANDARD_COLUMNS and n not in s.tables)
    report.extra_tables = sorted(t for t in s.tables if t not in rt.child_tables)
    return report


# ---------- the printed report ----------

def _count(report: TypeReport) -> str:
    if report.rows < 0:
        return "-"
    return f"{ROW_CAP}+" if report.rows > ROW_CAP else str(report.rows)


def _detail(report: TypeReport, show_extra: bool) -> list[str]:
    lines = [f"{report.key} ({report.doctype}): {report.verdict}"]
    if report.access == "denied":
        lines.append("  the key may not read this doctype: give its role Read permission, "
                     "or disable the type")
    elif report.access == "missing":
        lines.append("  this doctype does not exist on this ERP version: disable the type "
                     "in your profile")
    elif report.access == "failed":
        lines.append("  the ERP did not answer properly")
    elif report.verdict == "UNKNOWN":
        lines.append("  no records and no field list, so nothing could be checked: create one "
                     "test record in the ERP, then run the probe again")
    by_place: dict[str, list[Gap]] = {}
    for gap in report.gaps:
        by_place.setdefault(gap.place, []).append(gap)
    for place, gaps in by_place.items():
        names = ", ".join(g.name + (f" (similar: {', '.join(report.similar[g.name])})"
                                    if g.name in report.similar else "") for g in gaps)
        flag = "BLOCKS the type, " if any(g.blocking for g in gaps) else ""
        lines.append(f"  {flag}missing in {place}: {names}")
    if report.status_unknown:
        lines.append(f"  status values {report.status_basis} that the profile lacks: "
                     + ", ".join(report.status_unknown))
    lines += [f"  note: {n}" for n in report.notes]
    if show_extra and report.extra:
        shown = ", ".join(report.extra[:EXTRA_SHOWN])
        more = f" ... and {len(report.extra) - EXTRA_SHOWN} more" if len(report.extra) > EXTRA_SHOWN else ""
        lines.append(f"  fields on the ERP that the profile does not list ({len(report.extra)}): "
                     f"{shown}{more}")
    if show_extra and report.extra_tables:
        lines.append("  child tables not in the profile: " + ", ".join(report.extra_tables))
    return lines


def render(reports: list[TypeReport], show_extra: bool = False) -> str:
    lines = [f"{'Record type':30} {'DocType':30} {'Rows':>5}  {'Structure':10} Verdict"]
    for r in reports:
        lines.append(f"{r.key:30} {r.doctype:30} {_count(r):>5}  {r.source:10} {r.verdict}")
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    lines.append("")
    lines.append(", ".join(f"{n} {v}" for v, n in sorted(counts.items())))

    problems = [r for r in reports if r.verdict != "OK" or (show_extra and (r.extra or r.extra_tables))]
    if problems:
        lines.append("")
        for r in problems:
            lines += _detail(r, show_extra)
    confirmed = [r.key for r in reports if r.flagged_unverified and r.verdict == "OK"]
    open_ = [r.key for r in reports if r.flagged_unverified and r.verdict != "OK"]
    lines += ["", "Marked verify: true and now confirmed: " + (", ".join(confirmed) or "none"),
              "Marked verify: true and still open: " + (", ".join(open_) or "none"),
              "",
              "OK          every name in the profile exists on the ERP",
              "FIX         some names are missing (similar names are suggested) or statuses differ",
              "BLOCKED     the employee link or a fixed filter is missing: do not use this type yet",
              "UNKNOWN     no records and no field list: nothing could be checked",
              "UNAVAILABLE the key cannot read the doctype, or it does not exist"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a registry profile with the real ERP. Prints names and counts, "
                    "never record values (apart from the values of status fields).")
    parser.add_argument("--profile", help="profile name or file (default: REGISTRY_PROFILE)")
    parser.add_argument("--type", action="append", dest="types", help="check only this record type")
    parser.add_argument("--extra", action="store_true",
                        help="also list fields and child tables the profile does not use")
    args = parser.parse_args(argv)

    try:
        profile = load_profile(args.profile or settings.registry_profile)
    except RegistryError as err:
        print(f"The profile is invalid:\n{err}")
        return 2
    keys = list(profile.enabled_types())
    chosen = args.types or keys
    unknown = [k for k in chosen if k not in keys]
    if unknown:
        print(f"Unknown record types: {', '.join(unknown)}. Available: {', '.join(keys)}")
        return 2

    client = get_service_client()
    try:
        who = client.call_method("frappe.auth.get_logged_user")
    except FrappeError as exc:
        print(f"Cannot reach the ERP: {exc}")
        return 2
    print(f"Profile '{profile.name}', checked as '{who}'. No record values are printed.\n")

    reports = []
    for key in keys:
        if key in chosen:
            print(f"  checking {key} ...", file=sys.stderr, flush=True)
            reports.append(analyse(key, profile.get(key), observe(client, profile.get(key))))
    print(render(reports, show_extra=args.extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())