import pytest

from app.core.frappe_client import FrappeClient, FrappeError
from app.registry import build_profile, load_profile, probe
from app.registry.probe import (
    Observed, Structure, TableInfo, analyse, merge, observe, render,
    structure_from_meta, structure_from_row,
)

FIELDS = ["employee", "posting_date", "status", "amount"]


def make(**changes):
    rt = {"doctype": "Thing", "label": "Things", "description": "t",
          "aliases": {"en": ["thing"]}, "employee_field": "employee",
          "date_field": "posting_date", "status_field": "status",
          "status_values": ["Open", "Done"],
          "fields": ["name", "employee", "posting_date", "status", "amount"],
          "confidential_fields": ["amount"], "scopes": ["self"]}
    rt.update(changes)
    return build_profile({"name": "t", "record_types": {"thing": rt}}).get("thing")


def meta_doc(name, names, tables=None):
    fields = [{"fieldname": n, "fieldtype": "Data"} for n in names]
    for table, child in (tables or {}).items():
        fields.append({"fieldname": table, "fieldtype": "Table", "options": child})
    return {"name": name, "fields": fields}


def seen(names, **kw):
    return Observed(structure=Structure(source="meta", fields={n: "" for n in names},
                                        tables_known=True, **kw))


class FakeErp:
    def __init__(self, data=None, meta=None, docs=None, errors=None, meta_errors=None):
        self.data, self.meta, self.docs = data or {}, meta or {}, docs or {}
        self.errors, self.meta_errors, self.calls = errors or {}, meta_errors or {}, []

    def call_method(self, method, params=None):
        return "key@x.com"

    def get_all(self, doctype, fields=None, filters=None, order_by=None, max_rows=2000):
        self.calls.append(("get_all", doctype))
        if doctype in self.errors:
            raise self.errors[doctype]
        rows = self.data.get(doctype, [])
        if fields and fields != ["*"]:
            rows = [{f: r.get(f) for f in fields} for r in rows]
        return rows[:max_rows]

    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        self.calls.append(("get_list", doctype))
        return self.data.get(doctype, [])[:limit]

    def get_doc(self, doctype, name):
        self.calls.append(("get_doc", doctype))
        if (doctype, name) not in self.docs:
            raise FrappeError(404, "not found")
        return self.docs[(doctype, name)]

    def get_meta(self, doctype):
        self.calls.append(("get_meta", doctype))
        if doctype in self.meta_errors:
            raise self.meta_errors[doctype]
        return self.meta.get(doctype, [])


# ---------- reading structure ----------

def test_structure_from_a_sample_row_keeps_names_and_child_tables_only():
    row = {"name": "T1", "employee": "SENTINEL", "status": None}
    doc = {"lines": [{"name": "x", "item": "A"}], "empty_lines": []}
    found = structure_from_row(row, doc)
    assert set(found.fields) == {"name", "employee", "status", "lines", "empty_lines"}
    assert found.tables["lines"].fields == {"name", "item"}
    assert found.tables["empty_lines"].fields is None and found.tables_known
    assert not structure_from_row(row, None).tables_known


def test_structure_from_meta_skips_layout_and_reads_options_and_child_tables():
    docs = [{"name": "Thing", "fields": [
        {"fieldname": "section_1", "fieldtype": "Section Break"},
        {"fieldname": "status", "fieldtype": "Select", "options": "Open\nDone\n"},
        {"fieldname": "lines", "fieldtype": "Table", "options": "Thing Line"}]},
        meta_doc("Thing Line", ["item", "qty"])]
    found = structure_from_meta(docs, "Thing")
    assert "section_1" not in found.fields and "owner" in found.fields
    assert found.options["status"] == ["Open", "Done"]
    assert found.tables["lines"] == TableInfo("Thing Line", {"item", "qty"})
    assert structure_from_meta([], "Thing").source == "none"


def test_merging_keeps_everything_known():
    a = structure_from_row({"name": "T1", "x": 1}, {"lines": []})
    b = structure_from_meta([meta_doc("Thing", ["y"], {"lines": "Line"}), meta_doc("Line", ["q"])], "Thing")
    both = merge(a, b)
    assert both.source == "rows+meta" and {"x", "y"} <= set(both.fields)
    assert both.tables["lines"] == TableInfo("Line", {"q"})
    assert merge(Structure(), b) is b and merge(a, Structure()) is a


# ---------- comparing ----------

def test_a_matching_structure_is_ok():
    report = analyse("thing", make(), seen(FIELDS))
    assert report.verdict == "OK" and report.gaps == []


def test_a_missing_field_is_reported_with_a_similar_name():
    report = analyse("thing", make(), seen(["employee", "posting_date", "status", "total_amount"]))
    assert report.verdict == "FIX"
    assert [(g.place, g.name) for g in report.gaps] == [("fields", "amount")]
    assert report.similar["amount"] == ["total_amount"] and report.extra == ["total_amount"]


def test_a_missing_employee_link_or_fixed_filter_blocks_the_type():
    report = analyse("thing", make(), seen(["staff", "posting_date", "status", "amount"]))
    assert report.verdict == "BLOCKED"
    assert ("employee_field", "employee") in [(g.place, g.name) for g in report.gaps if g.blocking]
    filtered = make(base_filters=[["kind", "=", "Employee"]])
    assert analyse("thing", filtered, seen(FIELDS)).verdict == "BLOCKED"


def test_a_missing_date_or_status_field_is_a_fix_not_a_block():
    report = analyse("thing", make(), seen(["employee", "amount"]))
    places = {g.place for g in report.gaps}
    assert report.verdict == "FIX" and {"date_field", "status_field"} <= places


VIA = {"employee_field": None, "fields": ["name", "posting_date", "status"],
       "confidential_fields": [], "employee_via": {"child_doctype": "Thing Person",
                                                   "table_field": "people"},
       "child_tables": {"people": {"fields": ["employee", "employee_name"]}}}


def via_structure(info):
    return Structure(source="meta", tables_known=True,
                     fields={"posting_date": "", "status": "", "people": "Table"},
                     tables={"people": info})


@pytest.mark.parametrize("info, verdict", [
    (TableInfo("Thing Person", {"employee", "employee_name"}), "OK"),
    (TableInfo("Other Person", {"employee", "employee_name"}), "BLOCKED"),    # wrong child doctype
    (TableInfo("Thing Person", {"staff", "employee_name"}), "BLOCKED"),       # link field is missing
    (None, "BLOCKED"),                                                         # the table is missing
])
def test_the_link_through_a_child_table_is_checked(info, verdict):
    rt = make(**VIA)
    observed = Observed(structure=via_structure(info))
    if info is None:
        observed.structure.tables.clear()
    assert analyse("thing", rt, observed).verdict == verdict


def test_child_tables_of_the_profile_are_checked():
    rt = make(child_tables={"lines": {"fields": ["item", "qty"]}})
    lines = Structure(source="meta", tables_known=True, fields={n: "" for n in FIELDS},
                      tables={"lines": TableInfo("Line", {"item"})})
    report = analyse("thing", rt, Observed(structure=lines))
    assert [(g.place, g.name) for g in report.gaps] == [("table lines", "qty")]

    lines.tables.clear()
    assert [(g.place, g.name) for g in analyse("thing", rt, Observed(structure=lines)).gaps] \
        == [("child_tables", "lines")]

    lines.tables["lines"] = TableInfo(None, None)               # empty table, no field list
    report = analyse("thing", rt, Observed(structure=lines))
    assert report.verdict == "OK" and "could not be checked" in report.notes[0]


def test_status_values_are_compared_with_the_erp():
    structure = Structure(source="meta", tables_known=True, fields={n: "" for n in FIELDS},
                          options={"status": ["Open", "Done", "Cancelled"]})
    report = analyse("thing", make(), Observed(structure=structure))
    assert report.verdict == "FIX" and report.status_unknown == ["Cancelled"]
    assert report.status_basis == "allowed by the ERP"
    in_use = analyse("thing", make(), Observed(structure=Structure(
        source="rows", fields={n: "" for n in FIELDS}), statuses=["open", "Hold"]))
    assert in_use.status_unknown == ["Hold"] and in_use.status_basis == "in use"


def test_unreadable_and_unknown_types():
    assert analyse("thing", make(), Observed(access="denied", rows=-1)).verdict == "UNAVAILABLE"
    assert analyse("thing", make(), Observed(rows=0)).verdict == "UNKNOWN"


# ---------- looking at the ERP ----------

def test_observe_uses_a_sample_row_and_the_status_values():
    erp = FakeErp(data={"Thing": [
        {"name": "T1", "employee": "E1", "status": "Open", "other": "SENTINEL"},
        {"name": "T2", "employee": "E2", "status": "Done"}]})
    found = observe(erp, make())
    assert (found.access, found.rows, found.structure.source) == ("ok", 2, "rows")
    assert {"employee", "status", "other"} <= set(found.structure.fields)
    assert found.statuses == ["Done", "Open"]
    assert ("get_doc", "Thing") not in erp.calls                # no child tables: no record opened


def test_observe_reads_the_field_list_for_an_empty_type():
    erp = FakeErp(meta={"Thing": [meta_doc("Thing", ["employee", "status"])]})
    found = observe(erp, make())
    assert found.rows == 0 and found.structure.source == "meta"
    assert ("get_list", "Thing") not in erp.calls               # nothing to sample


def test_observe_falls_back_to_the_doctype_record():
    erp = FakeErp(docs={("DocType", "Thing"): {"fields": [
        {"fieldname": "employee", "fieldtype": "Link"}]}},
        meta_errors={"Thing": FrappeError(403, "no")})
    found = observe(erp, make())
    assert found.structure.source == "meta" and "employee" in found.structure.fields
    assert "the field list was not available (403)" in found.notes


def test_observe_opens_a_sample_record_only_when_the_profile_has_child_tables():
    erp = FakeErp(data={"Thing": [{"name": "T1", "employee": "E1"}]},
                  docs={("Thing", "T1"): {"name": "T1", "lines": [{"name": "x", "item": "A"}]}})
    found = observe(erp, make(child_tables={"lines": {"fields": ["item"]}}))
    assert ("get_doc", "Thing") in erp.calls
    assert found.structure.tables["lines"].fields == {"name", "item"}


def test_row_counts_are_capped_and_errors_are_labelled():
    big = FakeErp(data={"Thing": [{"name": f"T{i}", "status": "Open"} for i in range(500)]})
    assert observe(big, make()).rows == probe.ROW_CAP + 1
    for status, label in ((403, "denied"), (404, "missing"), (500, "failed"), (0, "failed")):
        erp = FakeErp(errors={"Thing": FrappeError(status, "x")})
        found = observe(erp, make())
        assert (found.access, found.rows) == (label, -1)


def test_a_missing_field_list_still_leaves_the_row_structure():
    erp = FakeErp(data={"Thing": [{"name": "T1", "employee": "E1"}]},
                  meta_errors={"Thing": FrappeError(403, "no")})
    assert observe(erp, make()).structure.source == "rows"


# ---------- the report ----------

def test_the_report_never_prints_record_values():
    erp = FakeErp(data={"Thing": [{"name": "T1", "employee": "SENTINEL_EMP",
                                   "amount": "SENTINEL_AMOUNT", "status": "Open",
                                   "posting_date": "SENTINEL_DATE", "extra": "SENTINEL_X"}]})
    rt = make()
    text = render([analyse("thing", rt, observe(erp, rt))], show_extra=True)
    assert "SENTINEL" not in text and "thing" in text


def test_the_report_names_what_to_do():
    rt = make(verify=True)
    ok = analyse("thing", rt, seen(FIELDS))
    broken = analyse("other", rt, seen(["employee", "posting_date", "status", "total_amount"]))
    text = render([ok, broken])
    assert "Marked verify: true and now confirmed: thing" in text
    assert "Marked verify: true and still open: other" in text
    assert "missing in fields: amount (similar: total_amount)" in text
    assert "1 FIX, 1 OK" in text


# ---------- the shipped profile ----------

def perfect_meta(rt):
    names = set(rt.fields) | {f for f, _, _ in rt.base_filters}
    names |= {x for x in (rt.employee_field, rt.date_field, rt.date_end_field, rt.status_field) if x}
    tables, children = {}, []
    for table, ct in rt.child_tables.items():
        child_name, child_fields = f"{rt.doctype} {table}", set(ct.fields)
        if rt.employee_via and table == rt.employee_via.table_field:
            child_name = rt.employee_via.child_doctype
            child_fields.add(rt.employee_via.child_field)
        tables[table] = child_name
        children.append(meta_doc(child_name, child_fields))
    main = meta_doc(rt.doctype, names, tables)
    for item in main["fields"]:
        if item["fieldname"] == rt.status_field:
            item.update(fieldtype="Select", options="\n".join(rt.status_values))
    return [main, *children]


def test_the_shipped_profile_agrees_with_a_structure_built_from_it():
    profile = load_profile("frappe_hrms_v16")
    erp = FakeErp(meta={rt.doctype: perfect_meta(rt) for rt in profile.enabled_types().values()})
    reports = {k: analyse(k, rt, observe(erp, rt)) for k, rt in profile.enabled_types().items()}
    assert {k: r.verdict for k, r in reports.items() if r.verdict != "OK"} == {}

    del_field = next(f for f in erp.meta["Salary Slip"][0]["fields"] if f["fieldname"] == "net_pay")
    erp.meta["Salary Slip"][0]["fields"].remove(del_field)
    slip = analyse("salary_slip", profile.get("salary_slip"), observe(erp, profile.get("salary_slip")))
    assert slip.verdict == "FIX" and [g.name for g in slip.gaps] == ["net_pay"]


# ---------- get_meta and the command ----------

class Replies:
    def __init__(self, body):
        self.body = body

    def get(self, url, **kwargs):
        body = self.body
        return type("Reply", (), {"status_code": 200, "reason": "OK", "text": "",
                                  "json": lambda self: body})()


@pytest.mark.parametrize("body, expected", [
    ({"docs": [{"name": "T"}]}, [{"name": "T"}]),
    ({"message": {"docs": [{"name": "T"}]}}, [{"name": "T"}]),
    ({"docs": [{"name": "T"}, "junk"]}, [{"name": "T"}]),
    ({"message": [1, 2]}, []), ({"docs": "no"}, []), ({}, []),
])
def test_get_meta_reads_the_docs_part_of_the_answer(body, expected):
    client = FrappeClient("http://x", "k", "s")
    client.session = Replies(body)
    assert client.get_meta("Thing") == expected


def command_profile():
    return build_profile({"name": "demo", "record_types": {"thing": {
        "doctype": "Thing", "label": "Things", "description": "t", "aliases": {"en": ["thing"]},
        "employee_field": "employee", "fields": ["name", "employee"]}}})


def test_the_command_prints_a_report(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_profile", lambda ref: command_profile())
    monkeypatch.setattr(probe, "get_service_client",
                        lambda: FakeErp(meta={"Thing": [meta_doc("Thing", ["employee"])]}))
    assert probe.main(["--profile", "x"]) == 0
    out = capsys.readouterr().out
    assert "checked as 'key@x.com'" in out and "thing" in out and "OK" in out


def test_the_command_refuses_unknown_types_and_bad_profiles(monkeypatch, capsys):
    monkeypatch.setattr(probe, "load_profile", lambda ref: command_profile())
    assert probe.main(["--type", "nope"]) == 2
    assert "Unknown record types: nope" in capsys.readouterr().out
    def broken(ref):
        raise probe.RegistryError("bad")
    monkeypatch.setattr(probe, "load_profile", broken)
    assert probe.main([]) == 2