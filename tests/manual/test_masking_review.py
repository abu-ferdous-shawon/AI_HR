from app.registry import build_profile, load_profile
from app.registry.review import masking_review


def one_type(**changes):
    base = {"doctype": "Thing", "label": "Things", "description": "t",
            "aliases": {"en": ["thing"]}, "employee_field": "employee",
            "fields": ["name", "employee", "status"], "scopes": ["self"]}
    base.update(changes)
    return build_profile({"name": "t", "record_types": {"thing": base}})


def test_the_shipped_profile_has_nothing_to_review():
    assert masking_review(load_profile("frappe_hrms_v16")) == []


def test_unmarked_free_text_is_reported_and_marking_it_clears_the_report():
    fields = ["name", "employee", "remarks", "notes"]
    problems = masking_review(one_type(fields=fields))
    assert any("thing.remarks" in p for p in problems)
    assert any("thing.notes" in p for p in problems)
    assert masking_review(one_type(fields=fields,
                                   confidential_fields=["remarks", "notes"])) == []


def test_free_text_in_a_child_table_is_reported():
    profile = one_type(child_tables={"rows": {"fields": ["line", "comment_text"]}})
    assert any("thing.rows.comment_text" in p for p in masking_review(profile))


def test_grouping_by_a_confidential_field_is_reported():
    profile = one_type(fields=["name", "employee", "code"], confidential_fields=["code"],
                       group_by_allowed=["code"])
    assert any("grouping by the confidential field 'code'" in p for p in masking_review(profile))


def test_totalling_a_confidential_field_without_the_money_flag_is_reported():
    profile = one_type(fields=["name", "employee", "code"], confidential_fields=["code"],
                       aggregations={"total_code": {"op": "sum", "field": "code"}})
    assert any("total_code" in p for p in masking_review(profile))