import copy
import json

import pytest

from app.registry import loader
from app.registry.loader import RegistryError, build_profile, load_profile


def thing(**changes):
    base = {
        "doctype": "Thing", "label": "Things", "description": "Test things.",
        "aliases": {"en": ["thing"], "bn": ["জিনিস"], "banglish": ["jinish"]},
        "employee_field": "employee", "date_field": "posting_date",
        "status_field": "status", "status_values": ["Open", "Done"], "docstatus": [1],
        "fields": ["name", "employee", "posting_date", "status", "amount", "kind"],
        "confidential_fields": ["amount"], "group_by_allowed": ["status", "kind"],
        "aggregations": {"total": {"op": "sum", "field": "amount", "money": True}},
        "order_by": "posting_date desc", "scopes": ["self", "company"],
    }
    base.update(changes)
    return base


def raw(**types):
    return {"name": "t", "record_types": {"thing": thing(), **types}}


def test_a_valid_profile_loads_with_defaults_filled():
    profile = build_profile(raw())
    rt = profile.get("thing")
    assert (rt.default_limit, rt.max_limit) == (20, 100)
    assert "count" in rt.aggregations                      # added automatically
    assert profile.employee.doctype == "Employee"


def test_aliases_resolve_in_every_language_and_ignore_case_and_spacing():
    profile = build_profile(raw())
    for text in ("thing", "  THING ", "জিনিস", "jinish", "Things", "thing"):
        assert profile.resolve(text) == "thing"
    assert profile.resolve("weather") is None


CASES = [
    ("no employee link", lambda t: t.pop("employee_field"), "exactly one"),
    ("two employee links", lambda t: t.update(employee_via={"child_doctype": "Child Thing", "table_field": "rows"}), "exactly one"),
    ("via without its table", lambda t: (t.pop("employee_field"), t.update(employee_via={"child_doctype": "Child Thing", "table_field": "rows"})), "must be a child table"),
    ("confidential outside fields", lambda t: t.update(confidential_fields=["secret"]), "confidential_fields must be listed"),
    ("group by outside fields", lambda t: t.update(group_by_allowed=["secret"]), "group_by_allowed must be listed"),
    ("aggregation field outside fields", lambda t: t.update(aggregations={"x": {"op": "sum", "field": "nope"}}), "not in fields"),
    ("sum without field", lambda t: t.update(aggregations={"x": {"op": "sum"}}), "needs a field"),
    ("money on a non confidential field", lambda t: t.update(confidential_fields=[]), "is money"),
    ("unsafe order by", lambda t: t.update(order_by="amount; drop table x"), "order_by is not safe"),
    ("order by a hidden column", lambda t: t.update(order_by="secret desc"), "order_by column"),
    ("date end without date", lambda t: (t.pop("date_field"), t.update(date_end_field="posting_date")), "date_end_field needs date_field"),
    ("status values without field", lambda t: t.update(status_field=None), "status_values needs status_field"),
    ("status field without values", lambda t: t.update(status_values=[]), "status_field needs status_values"),
    ("bad docstatus", lambda t: t.update(docstatus=[3]), "docstatus values"),
    ("unsafe doctype", lambda t: t.update(doctype="Thing; DROP"), "unsafe characters"),
    ("unsafe field name", lambda t: t.update(fields=["name", "Amount; x"]), "unsafe field name"),
    ("typo in a key", lambda t: t.update(scope=["self"]), "Extra inputs are not permitted"),
    ("unknown scope", lambda t: t.update(scopes=["everyone"]), "scopes"),
    ("bad filter operator", lambda t: t.update(base_filters=[["kind", "LIKE", "x"]]), "base_filters"),
    ("default above max", lambda t: t.update(default_limit=90, max_limit=50), "larger than max_limit"),
]


@pytest.mark.parametrize("label, change, expected", CASES, ids=[c[0] for c in CASES])
def test_invalid_profiles_are_rejected_with_a_clear_message(label, change, expected):
    data = raw()
    change(data["record_types"]["thing"])
    with pytest.raises(RegistryError) as err:
        build_profile(data)
    assert expected in str(err.value)


def test_an_alias_used_by_two_types_is_rejected():
    other = thing(doctype="Other", label="Others", aliases={"en": ["thing"]})
    with pytest.raises(RegistryError) as err:
        build_profile(raw(other=other))
    assert "is used by both" in str(err.value)


def test_a_disabled_type_does_not_block_its_aliases():
    other = thing(doctype="Other", label="Others", aliases={"en": ["thing"]}, enabled=False)
    assert build_profile(raw(other=other)).resolve("thing") == "thing"


def test_a_profile_with_nothing_enabled_is_rejected():
    data = {"name": "t", "record_types": {"thing": thing(enabled=False)}}
    with pytest.raises(RegistryError):
        build_profile(data)


def test_child_table_rules():
    bad = thing(child_tables={"rows": {"fields": ["a"], "confidential_fields": ["b"]}})
    with pytest.raises(RegistryError) as err:
        build_profile(raw(thing=bad))
    assert "child confidential_fields" in str(err.value)

    via = thing(employee_via={"child_doctype": "Child Thing", "table_field": "rows"},
                child_tables={"rows": {"fields": ["employee"]}})
    via.pop("employee_field")
    assert build_profile(raw(thing=via)).get("thing").employee_via.table_field == "rows"


def test_confidential_names_include_child_tables():
    t = thing(child_tables={"rows": {"fields": ["amount", "x"], "confidential_fields": ["amount"]}})
    assert "amount" in build_profile(raw(thing=t)).confidential_field_names()


# ---- files, overlays, cache ----

BASE_YAML = """
name: base
record_types:
  thing:
    doctype: Thing
    label: Things
    description: Test things.
    employee_field: employee
    fields: [name, employee, status]
    aliases: {en: [thing]}
"""


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_overlay_changes_only_what_it_names(tmp_path):
    write(tmp_path / "base.yaml", BASE_YAML)
    custom = write(tmp_path / "custom.yaml", """
extends: base.yaml
name: custom
record_types:
  thing: {label: Stuff}
  other:
    doctype: Other
    label: Others
    description: Another.
    employee_field: employee
    fields: [name, employee]
    aliases: {en: [other]}
""")
    profile = load_profile(custom)
    assert profile.name == "custom"
    assert profile.get("thing").label == "Stuff" and profile.get("thing").doctype == "Thing"
    assert profile.resolve("other") == "other"


def test_overlay_can_disable_a_shipped_type(tmp_path):
    custom = write(tmp_path / "acme.yaml",
                   "extends: frappe_hrms_v16\nname: acme\nrecord_types:\n  arrear: {enabled: false}\n")
    profile = load_profile(custom)
    assert "arrear" not in profile.enabled_types() and "salary_slip" in profile.enabled_types()


def test_circular_extends_is_refused(tmp_path):
    write(tmp_path / "a.yaml", "extends: b.yaml\nname: a\nrecord_types: {}\n")
    write(tmp_path / "b.yaml", "extends: a.yaml\nname: b\nrecord_types: {}\n")
    with pytest.raises(RegistryError) as err:
        load_profile(str(tmp_path / "a.yaml"))
    assert "Circular" in str(err.value)


def test_missing_and_broken_files_give_clear_errors(tmp_path):
    with pytest.raises(RegistryError) as err:
        load_profile("no_such_profile")
    assert "was not found" in str(err.value)
    broken = write(tmp_path / "broken.yaml", "name: [unclosed")
    with pytest.raises(RegistryError):
        load_profile(broken)
    not_a_mapping = write(tmp_path / "list.yaml", "- a\n- b\n")
    with pytest.raises(RegistryError):
        load_profile(not_a_mapping)


def test_json_profiles_work(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(raw()), encoding="utf-8")
    assert load_profile(str(path)).get("thing").doctype == "Thing"


def test_get_profile_follows_the_setting_and_is_cached(tmp_path, monkeypatch):
    path = write(tmp_path / "p.yaml", BASE_YAML)
    monkeypatch.setattr(loader.settings, "registry_profile", path)
    loader.clear_profile_cache()
    try:
        first = loader.get_profile()
        assert first.name == "base" and loader.get_profile() is first
    finally:
        loader.clear_profile_cache()


def test_loading_does_not_change_the_input():
    data = raw()
    before = copy.deepcopy(data)
    build_profile(data)
    assert data == before