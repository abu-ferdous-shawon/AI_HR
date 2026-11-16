import json

import pytest
import yaml

from app.core.frappe_client import FrappeError
from app.registry import draft, load_profile, load_with_overlay
from app.registry.probe import Gap, TypeReport
from tests.test_registry_probe import FakeErp, meta_doc

OTHER = {"doctype": "Other", "label": "Others", "description": "t",
         "aliases": {"en": ["other"]}, "employee_field": "employee",
         "fields": ["name", "employee"], "scopes": ["self"]}

VIA = {"employee_field": None, "confidential_fields": [], "group_by_allowed": [],
       "aggregations": {}, "order_by": "modified desc",
       "fields": ["name", "posting_date", "end_date", "status", "kind"],
       "child_tables": {"people": {"fields": ["employee", "employee_name"]}},
       "employee_via": {"child_doctype": "Thing Person", "table_field": "people"}}

THING_FIELDS = ["employee", "posting_date", "end_date", "status", "kind", "amount"]


def rt_raw(**changes):
    rt = {"doctype": "Thing", "label": "Things", "description": "t",
          "aliases": {"en": ["thing"]}, "employee_field": "employee",
          "date_field": "posting_date", "date_end_field": "end_date",
          "status_field": "status", "status_values": ["Open", "Done"],
          "fields": ["name", "employee", "posting_date", "end_date", "status", "amount", "kind"],
          "confidential_fields": ["amount"], "group_by_allowed": ["status", "kind"],
          "aggregations": {"total": {"op": "sum", "field": "amount", "money": True}},
          "child_tables": {"lines": {"fields": ["item", "qty", "price"],
                                     "confidential_fields": ["price"]}},
          "order_by": "posting_date desc, kind asc", "scopes": ["self"], "verify": True}
    rt.update(changes)
    return rt


def make_base(tmp_path, **changes) -> str:
    path = tmp_path / "base.json"
    path.write_text(json.dumps({"name": "base", "record_types": {
        "thing": rt_raw(**changes), "other": dict(OTHER)}}), encoding="utf-8")
    return str(path)


def report(gaps=(), access="ok", source="meta", rows=3, unknown=(), notes=(), verify=True):
    rep = TypeReport("thing", "Thing", access, rows, source, verify,
                     gaps=[g if isinstance(g, Gap) else Gap(*g) for g in gaps],
                     notes=list(notes))
    rep.status_unknown = list(unknown)
    return rep


def apply(tmp_path, rep, base_changes=None, **kw):
    """The change for 'thing' and the type you get after laying it over the base."""
    ref = make_base(tmp_path, **(base_changes or {}))
    change = draft.plan_type("thing", load_profile(ref).get("thing"), rep, **kw)
    merged = load_with_overlay(ref, {"name": "n", "record_types": {"thing": change.overlay}})
    return change, merged.record_types["thing"]


# ---------- planning ----------

def test_a_type_that_matches_only_has_its_verify_flag_cleared(tmp_path):
    change, merged = apply(tmp_path, report())
    assert (change.action, change.overlay) == ("confirmed", {"verify": False})
    assert merged.verify is False
    change, _ = apply(tmp_path, report(verify=False), base_changes={"verify": False})
    assert (change.action, change.overlay) == ("unchanged", {})


def test_missing_fields_are_dropped_everywhere_and_the_result_stays_valid(tmp_path):
    change, merged = apply(tmp_path, report([("fields", "amount"), ("fields", "kind")]))
    assert change.action == "adjusted"
    assert "amount" not in merged.fields and "kind" not in merged.fields
    assert merged.confidential_fields == [] and merged.group_by_allowed == ["status"]
    assert set(merged.aggregations) == {"count"}                 # the total of 'amount' is gone
    assert merged.order_by == "posting_date desc" and merged.verify is False


def test_a_missing_date_field_clears_the_period_filter_and_the_sort(tmp_path):
    rep = report([("fields", "posting_date"), ("date_field", "posting_date"), ("fields", "kind")])
    _, merged = apply(tmp_path, rep)
    assert merged.date_field is None and merged.date_end_field is None
    assert merged.order_by == "modified desc"


def test_a_missing_end_date_keeps_the_start_date(tmp_path):
    _, merged = apply(tmp_path, report([("fields", "end_date"), ("date_end_field", "end_date")]))
    assert merged.date_field == "posting_date" and merged.date_end_field is None


def test_a_missing_status_field_removes_the_status_filter(tmp_path):
    rep = report([("fields", "status"), ("status_field", "status")], unknown=["Hold"])
    _, merged = apply(tmp_path, rep)
    assert merged.status_field is None and merged.status_values == []
    assert merged.group_by_allowed == ["kind"]


def test_statuses_the_erp_uses_are_added(tmp_path):
    change, merged = apply(tmp_path, report(unknown=["Cancelled", "Hold"]))
    assert merged.status_values == ["Open", "Done", "Cancelled", "Hold"]
    assert change.action == "adjusted"


def test_child_table_names_are_dropped_or_the_whole_table_removed(tmp_path):
    _, merged = apply(tmp_path, report([("table lines", "price")]))
    assert merged.child_tables["lines"].fields == ["item", "qty"]
    assert merged.child_tables["lines"].confidential_fields == []
    _, merged = apply(tmp_path, report([("table lines", n) for n in ("item", "qty", "price")]))
    assert "lines" not in merged.child_tables
    _, merged = apply(tmp_path, report([("child_tables", "lines")]))
    assert "lines" not in merged.child_tables


def test_types_that_cannot_work_are_disabled(tmp_path):
    change, merged = apply(tmp_path, report([Gap("employee_field", "employee", True)]))
    assert (change.action, merged.enabled) == ("disabled", False)
    assert "employee_field" in change.notes[0]
    change, merged = apply(tmp_path, report(access="missing", rows=-1))
    assert merged.enabled is False and "does not exist" in change.notes[0]


def test_unreadable_and_unchecked_types_are_kept_unless_asked(tmp_path):
    denied = report(access="denied", rows=-1)
    change, merged = apply(tmp_path, denied)
    assert (change.action, change.overlay, merged.enabled) == ("kept", {}, True)
    assert apply(tmp_path, denied, disable_denied=True)[1].enabled is False
    for rep in (report(source="none", rows=0), report(access="failed", rows=-1)):
        change, _ = apply(tmp_path, rep)
        assert (change.action, change.overlay) == ("kept", {})


def test_a_missing_link_table_disables_the_type(tmp_path):
    change, merged = apply(tmp_path, report([("child_tables", "people")]), base_changes=VIA)
    assert change.action == "disabled" and merged.enabled is False
    _, merged = apply(tmp_path, report([("table people", "employee_name")]), base_changes=VIA)
    assert merged.enabled and merged.child_tables["people"].fields == ["employee"]


def test_unchecked_child_tables_keep_the_verify_flag(tmp_path):
    change, merged = apply(tmp_path, report(notes=["child tables could not be checked (x)"]))
    assert "verify" not in change.overlay and merged.verify is True
    assert change.action == "kept" and "still marked verify" in change.notes[0]


# ---------- every shipped type survives the worst case ----------

SHIPPED = load_profile("frappe_hrms_v16")


@pytest.mark.parametrize("key", list(SHIPPED.enabled_types()))
def test_every_shipped_type_stays_valid_when_every_optional_name_is_missing(key):
    rt = SHIPPED.get(key)
    keep = {rt.employee_via.child_field} if rt.employee_via else set()
    gaps = [Gap("fields", f) for f in rt.fields if f != "name"]
    for place, name in (("date_field", rt.date_field), ("date_end_field", rt.date_end_field),
                        ("status_field", rt.status_field)):
        if name:
            gaps.append(Gap(place, name))
    for table, ct in rt.child_tables.items():
        gaps += [Gap(f"table {table}", f) for f in ct.fields if f not in keep]
    rep = TypeReport(key, rt.doctype, "ok", 3, "meta", rt.verify, gaps=gaps)
    rep.status_unknown = ["Brand New"] if rt.status_field else []

    change = draft.plan_type(key, rt, rep)
    assert change.action in {"adjusted", "disabled"}
    merged = load_with_overlay("frappe_hrms_v16",
                               {"name": "n", "record_types": {key: change.overlay}})
    assert key in merged.record_types                      # still a valid profile


# ---------- the file ----------

def test_the_rendered_yaml_reads_back_to_the_same_overlay(tmp_path):
    base = load_profile(make_base(tmp_path))
    rep = report([("fields", "amount"), ("fields", "status"), ("status_field", "status"),
                  ("table lines", "price")])
    changes = [draft.plan_type("thing", base.get("thing"), rep),
               draft.TypeChange("other", "disabled", {"enabled": False}, ["demo: it is gone"])]
    overlay = draft.build_overlay("Starkology: draft", changes)
    text = draft.render_yaml("frappe_hrms_v16", overlay, changes)
    assert yaml.safe_load(text) == {"extends": "frappe_hrms_v16", **overlay}
    assert "# thing: dropped fields the ERP does not have: amount, status" in text
    assert "# other: demo: it is gone" in text


def test_an_overlay_without_changes_renders_an_empty_mapping():
    overlay = draft.build_overlay("n", [draft.TypeChange("thing", "unchanged")])
    assert yaml.safe_load(draft.render_yaml("x", overlay, [])) == {
        "extends": "x", "name": "n", "record_types": {}}


# ---------- the command ----------

def erp(missing=None, **kw):
    names = [f for f in THING_FIELDS if f != missing]
    return FakeErp(meta={"Thing": [meta_doc("Thing", names, {"lines": "Thing Line"}),
                                   meta_doc("Thing Line", ["item", "qty", "price"])],
                         "Other": [meta_doc("Other", ["employee"])]}, **kw)


def gone_and_missing():
    return erp(missing="amount", errors={"Other": FrappeError(404, "x")})


def test_a_dry_run_shows_the_changes_and_writes_nothing(tmp_path, monkeypatch, capsys):
    base = make_base(tmp_path, verify=False)
    monkeypatch.setattr(draft, "get_service_client", gone_and_missing)
    assert draft.main(["--profile", base]) == 0
    out = capsys.readouterr().out
    assert "DISABLED (1)" in out and "other: the doctype does not exist" in out
    assert "dropped fields the ERP does not have: amount" in out and "nothing was written" in out
    assert [p.name for p in tmp_path.iterdir()] == ["base.json"]


def test_writing_saves_a_profile_that_loads_and_will_not_overwrite(tmp_path, monkeypatch, capsys):
    base = make_base(tmp_path, verify=False)
    monkeypatch.setattr(draft, "get_service_client", gone_and_missing)
    target = tmp_path / "sub" / "draft.yaml"
    assert draft.main(["--profile", base, "--write", str(target), "--name", "acme"]) == 0
    profile = load_profile(str(target))
    assert profile.name == "acme" and "other" not in profile.enabled_types()
    assert "amount" not in profile.get("thing").fields
    assert draft.main(["--profile", base, "--write", str(target)]) == 2
    assert "already exists" in capsys.readouterr().out
    assert draft.main(["--profile", base, "--write", str(target), "--force"]) == 0


def test_nothing_is_written_when_nothing_needs_changing(tmp_path, monkeypatch, capsys):
    base = make_base(tmp_path, verify=False)
    monkeypatch.setattr(draft, "get_service_client", erp)
    target = tmp_path / "x.yaml"
    assert draft.main(["--profile", base, "--write", str(target)]) == 0
    assert "No changes are needed" in capsys.readouterr().out and not target.exists()


def test_bad_input_is_refused(tmp_path, monkeypatch, capsys):
    base = make_base(tmp_path)
    monkeypatch.setattr(draft, "get_service_client", erp)
    assert draft.main(["--profile", base, "--type", "nope"]) == 2
    assert "Unknown record types: nope" in capsys.readouterr().out
    assert draft.main(["--profile", str(tmp_path / "missing.json")]) == 2