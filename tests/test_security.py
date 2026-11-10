import pytest

from app.core import security
from app.core.security import AccessDenied, UserContext, require_access, scrub

MANAGER = UserContext("mgr@x.com", "HR-EMP-00004")
REPORT = UserContext("rep@x.com", "HR-EMP-00001")
TEAM = [{"name": "HR-EMP-00001", "employee_name": "Report"}]


@pytest.fixture
def team(monkeypatch):
    """Pretend the manager has exactly one direct report."""
    monkeypatch.setattr(security, "get_team",
                        lambda ctx: TEAM if ctx.employee_id == "HR-EMP-00004" else [])


def test_scrub_removes_confidential_fields_even_when_nested():
    data = {"name": "X", "cell_number": "1", "inner": {"bank_ac_no": "2", "ok": 1}}
    assert scrub(data) == {"name": "X", "inner": {"ok": 1}}


def test_scrub_catches_custom_fields_by_name():
    data = {"guardian_phone": "1", "nid_number": "2", "department": "IT"}
    assert scrub(data) == {"department": "IT"}


def test_scrub_allow_unlocks_only_what_is_named():
    data = {"cell_number": "1", "date_of_birth": "2000-01-01"}
    assert scrub(data, frozenset({"cell_number"})) == {"cell_number": "1"}


def test_scrub_star_lets_everything_through():
    data = {"cell_number": "1", "bank_ac_no": "2"}
    assert scrub(data, frozenset({"*"})) == data


def test_no_id_or_own_id_means_myself(team):
    assert require_access(MANAGER, None) == "HR-EMP-00004"
    assert require_access(MANAGER, "HR-EMP-00004") == "HR-EMP-00004"


def test_manager_may_use_a_direct_reports_id(team):
    assert require_access(MANAGER, "HR-EMP-00001") == "HR-EMP-00001"


def test_manager_is_refused_an_outsider(team):
    with pytest.raises(AccessDenied):
        require_access(MANAGER, "HR-EMP-00003")


def test_a_report_cannot_reach_their_manager(team):
    with pytest.raises(AccessDenied):
        require_access(REPORT, "HR-EMP-00004")