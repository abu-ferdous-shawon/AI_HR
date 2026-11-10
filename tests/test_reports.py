import csv

import pytest

from app.core import reports, security
from app.core.reports import get_report, save_report
from app.core.security import UserContext
from app.tools.registry import run_tool

MGR = UserContext("m@x.com", "HR-EMP-00004")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(reports, "REPORT_DIR", tmp_path / "reports")
    reports._reports.clear()


def test_csv_report_is_written_and_formulas_are_neutralised():
    info = save_report("u1", "Test report", ["A", "B"],
                       [['=HYPERLINK("x")', 5], ["ok", 6]], "csv")
    stored = get_report(info["id"], "u1")
    rows = list(csv.reader(stored.path.open(encoding="utf-8-sig")))
    assert rows[0] == ["A", "B"]
    assert rows[1][0].startswith("'=")           # no longer a formula
    assert info["name"].endswith(".csv") and info["rows"] == 2


def test_excel_report_has_header_and_rows():
    openpyxl = pytest.importorskip("openpyxl")
    info = save_report("u1", "Test", ["A"], [["x"], ["y"]], "excel")
    book = openpyxl.load_workbook(get_report(info["id"], "u1").path)
    assert [c.value for c in book.active["A"]] == ["A", "x", "y"]


def test_someone_elses_report_is_not_available():
    info = save_report("u1", "Test", ["A"], [["x"]], "csv")
    assert get_report(info["id"], "u2") is None


def test_expired_reports_are_removed(monkeypatch):
    info = save_report("u1", "Test", ["A"], [["x"]], "csv")
    monkeypatch.setattr(reports, "TTL_SECONDS", -1)
    assert get_report(info["id"], "u1") is None


def test_report_tool_refuses_an_outsider(monkeypatch):
    monkeypatch.setattr(security, "get_team", lambda ctx: [])
    res = run_tool("create_report",
                   {"report_type": "attendance", "employee_id": "HR-EMP-00003"}, MGR)
    assert "error" in res


def test_only_attendance_can_cover_other_people(monkeypatch):
    team = [{"name": "HR-EMP-00001", "employee_name": "R"}]
    monkeypatch.setattr(security, "get_team", lambda ctx: team)
    res = run_tool("create_report",
                   {"report_type": "payslips", "employee_id": "HR-EMP-00001"}, MGR)
    assert "error" in res


def test_unknown_report_type_is_refused():
    assert "error" in run_tool("create_report", {"report_type": "everything"}, MGR)