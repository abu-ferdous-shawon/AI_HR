import pytest

from app.core import masking
from app.core.frappe_client import FrappeError
from app.core.masking import Vault, register_confidential
from app.core.security import AccessDenied, ToolInputError
from app.records import engine
from app.registry import load_profile
from tests.records_helpers import World, leave_row


@pytest.fixture
def world(monkeypatch):
    saved = set(masking.EXTRA_CONFIDENTIAL)
    yield World(monkeypatch)
    masking.EXTRA_CONFIDENTIAL.clear()
    masking.EXTRA_CONFIDENTIAL.update(saved)


# ---------- listing: outcomes ----------

def test_own_records_are_projected_and_limited_to_the_caller(world):
    world.rows["Leave Thing"] = [leave_row("L1"), leave_row("L2", "Open", reason="")]
    res = world.fetch()
    assert (res["outcome"], res["count"], res["truncated"]) == ("records", 2, False)
    first, second = res["records"]
    assert "secret_col" not in first and "owner" not in first       # not in the allowlist
    assert first["employee_name"] == "Me" and "reason" not in second  # trimmed; empty dropped
    call = world.all_calls[0]
    assert ["employee", "=", "EMP-1"] in call["filters"]
    assert ["docstatus", "in", [0, 1]] in call["filters"]
    assert call["max_rows"] == 3                                    # default limit 2, plus one
    assert res["filters_used"]["scope"] == "self"
    assert res["filters_used"]["record_state"] == ["draft", "submitted"]
    assert res["source_doctype"] == "Leave Thing"


def test_empty_is_no_records_with_the_filters_shown(world):
    res = world.fetch(status="open")
    assert res["outcome"] == "no_records" and "records" not in res
    assert res["filters_used"]["status"] == "Open" and "message" in res


def test_denied_by_the_erp_and_failed_are_different(world):
    world.error = FrappeError(403, "no permission")
    with pytest.raises(AccessDenied, match="did not allow access"):
        world.fetch()
    world.error = FrappeError(401, "no login")
    with pytest.raises(AccessDenied):
        world.fetch()
    world.error = FrappeError(500, "boom")
    with pytest.raises(FrappeError):
        world.fetch()


def test_unknown_types_are_refused_and_aliases_work(world):
    with pytest.raises(ToolInputError, match="Unknown record type"):
        world.fetch("weather")
    with pytest.raises(ToolInputError, match="record_type is required"):
        world.fetch(None)
    world.rows["Leave Thing"] = [leave_row("L1")]
    assert world.fetch("CHUTI")["outcome"] == "records"


# ---------- listing: size ----------

def test_truncation_is_reported(world):
    world.rows["Leave Thing"] = [leave_row(f"L{i}") for i in range(4)]
    res = world.fetch()
    assert res["count"] == 2 and res["truncated"] is True
    world.rows["Leave Thing"] = [leave_row("L1"), leave_row("L2")]
    assert world.fetch()["truncated"] is False


def test_over_limit_requests_are_capped_and_bad_limits_refused(world):
    world.rows["Leave Thing"] = [leave_row(f"L{i}") for i in range(10)]
    res = world.fetch(limit=50)
    assert res["limit"] == 3 and res["limit_capped"] is True and res["count"] == 3
    assert world.all_calls[-1]["max_rows"] == 4
    for bad in (0, -1, "many"):
        with pytest.raises(ToolInputError):
            world.fetch(limit=bad)


# ---------- listing: dates and status ----------

def test_dates_select_overlapping_records(world):
    world.fetch(from_date="2026-03-01", to_date="2026-03-31")
    assert ["from_date", "<=", "2026-03-31"] in world.filters
    assert ["to_date", ">=", "2026-03-01"] in world.filters
    assert world.fetch(from_date="2026-03-01")["filters_used"]["period"]["match"] == "overlapping"


def test_datetime_columns_cover_whole_days(world):
    world.fetch("punch", from_date="2026-03-01", to_date="2026-03-31")
    assert ["time", ">=", "2026-03-01 00:00:00"] in world.filters
    assert ["time", "<=", "2026-03-31 23:59:59"] in world.filters


def test_bad_dates_and_statuses_are_refused(world):
    with pytest.raises(ToolInputError):
        world.fetch(from_date="yesterday")
    with pytest.raises(ToolInputError, match="must not be after"):
        world.fetch(from_date="2026-04-01", to_date="2026-03-01")
    with pytest.raises(ToolInputError, match="Open, Approved"):
        world.fetch(status="maybe")


def test_filters_that_do_not_apply_are_ignored_and_reported(world):
    res = world.fetch("event", from_date="2026-01-01", status="Approved")
    assert res["outcome"] == "no_records"
    assert res["filters_used"]["ignored"] == ["from_date", "status"]


# ---------- scope and access ----------

def test_company_scope_needs_hr(world):
    with pytest.raises(AccessDenied, match="HR only"):
        world.fetch(scope="company")
    world.hr = True
    world.fetch(scope="company")
    assert not any(f[0] == "employee" for f in world.filters)       # everyone


def test_team_scope_needs_direct_reports(world):
    with pytest.raises(AccessDenied, match="no direct reports"):
        world.fetch(scope="team")
    world.team = [{"name": "EMP-2", "employee_name": "Rahim Uddin "},
                  {"name": "EMP-3", "employee_name": "Karim Ali"}]
    world.fetch(scope="team")
    assert ["employee", "in", ["EMP-2", "EMP-3"]] in world.filters


def test_a_scope_the_type_does_not_allow_is_denied(world):
    world.team = [{"name": "EMP-2", "employee_name": "Rahim"}]
    with pytest.raises(AccessDenied, match="not available at the 'team' level"):
        world.fetch("slip", scope="team")
    with pytest.raises(ToolInputError, match="scope must be"):
        world.fetch(scope="everyone")


def test_reading_another_person_in_self_scope_is_denied(world):
    world.employees = [{"name": "EMP-1", "employee_name": "Me Myself"}]
    with pytest.raises(AccessDenied, match="only view your own"):
        world.fetch(employee="Rahim")
    assert world.fetch(employee="me")["outcome"] == "no_records"    # asking for yourself is fine


def test_a_manager_cannot_read_a_non_report(world):
    world.team = [{"name": "EMP-2", "employee_name": "Rahim Uddin"}]
    with pytest.raises(AccessDenied, match="not one of your direct reports"):
        world.fetch(scope="team", employee="Karim")
    res = world.fetch(scope="team", employee="rahim")
    assert ["employee", "=", "EMP-2"] in world.filters
    assert res["employee"] == {"id": "EMP-2", "name": "Rahim Uddin"}


def test_ambiguous_and_missing_names_are_reported(world):
    world.team = [{"name": "EMP-2", "employee_name": "Rahim Uddin"},
                  {"name": "EMP-3", "employee_name": "Rahim Khan"}]
    with pytest.raises(ToolInputError, match="More than one employee"):
        world.fetch(scope="team", employee="rahim")
    world.hr, world.employees = True, []
    with pytest.raises(ToolInputError, match="No employee matches"):
        world.fetch(scope="company", employee="Nobody")
    world.employees = [{"name": "EMP-7", "employee_name": "Rahim Uddin"}]
    world.fetch(scope="company", employee="Rahim")
    assert ["employee", "=", "EMP-7"] in world.filters
    world.employees = [{"name": "EMP-7", "employee_name": "A"}, {"name": "EMP-8", "employee_name": "B"}]
    with pytest.raises(ToolInputError, match="More than one employee"):
        world.fetch(scope="company", employee="x")


# ---------- details and masking ----------

SLIP = {"name": "S1", "employee": "EMP-1", "start_date": "2026-03-01",
        "net_pay": 50000.0, "dept": "IT"}
SLIP_DOC = {"earnings": [{"salary_component": "Basic", "amount": 61000.0},
                         {"salary_component": "HRA", "amount": 7000.0},
                         {"salary_component": "Bonus", "amount": 900.0}]}


def test_line_items_are_capped_flattened_and_masked(world):
    world.rows["Pay Thing"], world.docs["S1"] = [SLIP], SLIP_DOC
    res = world.fetch("slip")
    record = res["records"][0]
    assert res["details_included"] is True and world.doc_calls == [("Pay Thing", "S1")]
    assert [i["section"] for i in record["line_items"]] == ["earnings", "earnings"]
    assert record["line_items_truncated"] == ["earnings"]
    register_confidential(world.profile.confidential_field_names())
    seen = str(Vault().mask(res))
    assert "Basic" in seen and "HRA" in seen
    for secret in ("50000", "61000", "7000"):
        assert secret not in seen


def test_details_are_skipped_for_many_records_and_survive_a_failure(world, monkeypatch):
    world.rows["Pay Thing"] = [dict(SLIP, name="S1"), dict(SLIP, name="S2")]
    monkeypatch.setattr(engine, "DETAIL_MAX", 1)
    res = world.fetch("slip")
    assert res["details_included"] is False and world.doc_calls == []
    monkeypatch.setattr(engine, "DETAIL_MAX", 5)
    world.doc_error = FrappeError(500, "boom")
    res = world.fetch("slip")
    assert res["outcome"] == "records" and res["details_included"] is False
    assert all("line_items" not in r for r in res["records"])


def test_co_attendees_are_never_shown_to_a_normal_user(world):
    world.rows["Event Thing"] = [{"name": "E1", "title": "Course"}]
    world.docs["E1"] = {"people": [{"employee": "EMP-1", "employee_name": "Me"},
                                   {"employee": "EMP-9", "employee_name": "Other Person"}]}
    res = world.fetch("event")
    assert ["Event Person", "employee", "=", "EMP-1"] in world.filters
    assert res["records"][0]["line_items"] == [
        {"section": "people", "employee": "EMP-1", "employee_name": "Me"}]
    assert "Other Person" not in str(res)
    world.hr = True
    assert len(world.fetch("event", scope="company")["records"][0]["line_items"]) == 2


def test_profile_confidential_fields_are_masked_for_the_model(world):
    world.profile = load_profile("frappe_hrms_v16")
    register_confidential(world.profile.confidential_field_names())
    world.rows["Leave Application"] = [{"name": "L1", "employee": "EMP-1", "leave_type": "Sick Leave",
                                        "description": "Fever at home", "status": "Approved"}]
    res = world.fetch("leave_application")
    assert res["records"][0]["description"] == "Fever at home"
    assert "Fever" not in str(Vault().mask(res))


# ---------- totals ----------

def test_sums_are_exact_and_skip_bad_values(world):
    world.rows["Leave Thing"] = [leave_row("L1", days=0.1), leave_row("L2", days=0.2),
                                 leave_row("L3", days="oops"), leave_row("L4", days=None),
                                 leave_row("L5", days=1.0)]
    res = world.summarize(aggregation="TOTAL_DAYS")
    assert res["value"] == 1.3 and res["records_counted"] == 5
    assert res["numeric_records"] == 3 and res["skipped_non_numeric"] == 1
    assert res["operation"] == "sum" and res["source_doctype"] == "Leave Thing"


def test_count_average_and_max(world):
    world.rows["Leave Thing"] = [leave_row("L1", days=2.0), leave_row("L2", days=1.0),
                                 leave_row("L3", "Open", days=1.0)]
    assert world.summarize()["value"] == 3 and world.summarize()["aggregation"] == "count"
    assert world.summarize(aggregation="avg_days")["value"] == 1.33
    assert world.summarize(aggregation="max_days")["value"] == 2.0


def test_statuses_and_period_are_stated(world):
    world.rows["Leave Thing"] = [leave_row("L1"), leave_row("L2", "Open")]
    res = world.summarize(from_date="2026-03-01", to_date="2026-03-31")
    assert res["statuses_included"] == ["Approved", "Open"]
    assert res["period"]["from_date"] == "2026-03-01"
    assert world.summarize(status="open")["statuses_included"] == ["Open"]


def test_groups_are_sorted_and_labelled(world):
    world.rows["Leave Thing"] = [leave_row("L1", days=2.0), leave_row("L2", days=1.0),
                                 leave_row("L3", "Open", days=1.0)]
    res = world.summarize(aggregation="total_days", group_by="status")
    assert res["groups"] == [{"group": "Approved", "records": 2, "value": 3.0},
                             {"group": "Open", "records": 1, "value": 1.0}]


def test_money_is_exact_grouped_and_hidden_from_the_model(world):
    world.rows["Pay Thing"] = [dict(SLIP, name="S1", net_pay=50000.0),
                               dict(SLIP, name="S2", net_pay=40000.5, dept="HR")]
    res = world.summarize("slip", aggregation="total_net", group_by="dept")
    assert res["amount"] == 90000.5 and "value" not in res
    assert res["groups"][0] == {"group": "IT", "records": 1, "amount": 50000.0}
    seen = str(Vault().mask(res))
    assert "90000" not in seen and "50000" not in seen and "40000" not in seen


def test_shipped_profile_totals_use_decimal_arithmetic(world):
    world.profile = load_profile("frappe_hrms_v16")
    world.rows["Salary Slip"] = [{"name": "S1", "employee": "EMP-1", "gross_pay": "60000.10"},
                                 {"name": "S2", "employee": "EMP-1", "gross_pay": 0.2, "net_pay": None}]
    res = world.summarize("salary_slip", aggregation="total_gross")
    assert res["amount"] == 60000.3 and res["source_doctype"] == "Salary Slip"
    assert "60000" not in str(Vault().mask(res))


def test_summaries_with_no_rows_or_too_many_rows(world):
    res = world.summarize()
    assert res["outcome"] == "no_records" and "value" not in res
    world.rows["Leave Thing"] = [leave_row(f"L{i}") for i in range(101)]
    with pytest.raises(ToolInputError, match="too many"):
        world.summarize()


def test_invalid_aggregation_and_group_by_list_the_valid_ones(world):
    with pytest.raises(ToolInputError, match="total_days"):
        world.summarize(aggregation="nope")
    with pytest.raises(ToolInputError, match="status"):
        world.summarize(group_by="employee")
    with pytest.raises(ToolInputError, match="cannot be grouped"):
        world.summarize("event", group_by="title")


def test_summaries_follow_the_same_scope_rules(world):
    with pytest.raises(AccessDenied, match="HR only"):
        world.summarize(scope="company")
    world.error = FrappeError(500, "boom")
    with pytest.raises(FrappeError):
        world.summarize()