import pytest

from app.registry.loader import load_profile

EXPECTED_DOCTYPES = {
    "Attendance", "Attendance Request", "Employee Checkin", "Leave Application",
    "Leave Allocation", "Leave Policy Assignment", "Holiday List Assignment",
    "Employee Onboarding", "Employee Transfer", "Employee Promotion",
    "Employee Grievance", "Employee Separation", "Exit Interview",
    "Full and Final Statement", "Salary Withholding", "Shift Request",
    "Shift Assignment", "Expense Claim", "Employee Benefit Application",
    "Employee Benefit Claim", "Salary Structure Assignment", "Salary Slip",
    "Additional Salary", "Timesheet", "Employee Incentive", "Retention Bonus",
    "Overtime Slip", "Arrear", "Payroll Correction", "Training Event",
    "Training Result", "Training Feedback", "Employee Skill Map", "Appraisal",
    "Contract",
}

# Policy: these never get manager (team) access in service-account mode.
PAYROLL = ["salary_slip", "salary_structure_assignment", "additional_salary",
           "employee_incentive", "retention_bonus", "overtime_slip", "arrear",
           "payroll_correction", "salary_withholding", "full_and_final_statement",
           "employee_benefit_application", "employee_benefit_claim", "expense_claim"]
SENSITIVE = ["employee_grievance", "exit_interview", "employee_separation",
             "appraisal", "contract"]


@pytest.fixture(scope="module")
def profile():
    return load_profile("frappe_hrms_v16")


def test_every_requested_doctype_is_covered_once(profile):
    doctypes = [rt.doctype for rt in profile.record_types.values()]
    assert set(doctypes) == EXPECTED_DOCTYPES and len(doctypes) == 35


def test_every_type_is_enabled_and_allows_self_access(profile):
    assert len(profile.enabled_types()) == 35
    assert all("self" in rt.scopes for rt in profile.record_types.values())


def test_every_type_can_be_named_in_english_bangla_and_banglish(profile):
    for key, rt in profile.record_types.items():
        assert rt.aliases.en and rt.aliases.bn and rt.aliases.banglish, key


@pytest.mark.parametrize("text, key", [
    ("hajira", "attendance"), ("হাজিরা", "attendance"),
    ("Payslip", "salary_slip"), ("বেতন স্লিপ", "salary_slip"),
    ("  SALARY   SLIP ", "salary_slip"), ("chutir abedon", "leave_application"),
    ("ছুটির আবেদন", "leave_application"), ("reimbursement", "expense_claim"),
    ("khoroch", "expense_claim"), ("podonnoti", "employee_promotion"),
    ("performance review", "appraisal"), ("expense_claim", "expense_claim"),
    ("f&f", "full_and_final_statement"), ("proshikkhon", "training_event"),
])
def test_aliases_in_three_languages_resolve(profile, text, key):
    assert profile.resolve(text) == key


def test_payroll_and_sensitive_records_never_allow_team_scope(profile):
    for key in PAYROLL + SENSITIVE:
        assert "team" not in profile.get(key).scopes, key


def test_money_fields_are_confidential(profile):
    slip = profile.get("salary_slip")
    assert {"gross_pay", "net_pay", "total_deduction"} <= set(slip.confidential_fields)
    assert "amount" in slip.child_tables["earnings"].confidential_fields
    for key, rt in profile.record_types.items():
        for name, agg in rt.aggregations.items():
            if agg.money:
                assert agg.field in rt.confidential_fields, f"{key}.{name}"


def test_child_table_links_filter_co_attendees(profile):
    for key in ("training_event", "training_result"):
        rt = profile.get(key)
        assert rt.employee_via.table_field in rt.child_tables, key


def test_limits_are_resolved_and_count_is_always_available(profile):
    for key, rt in profile.record_types.items():
        assert rt.default_limit <= rt.max_limit, key
        assert "count" in rt.aggregations, key


def test_masking_gets_the_confidential_names_and_unverified_types_are_listed(profile):
    assert {"gross_pay", "description", "bonus_amount"} <= profile.confidential_field_names()
    unverified = profile.unverified()
    assert "arrear" in unverified and "attendance" not in unverified
    assert {c["key"] for c in profile.catalog()} == set(profile.record_types)