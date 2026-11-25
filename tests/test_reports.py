import csv

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.auth.credentials import Credentials, current_holder, use_credentials
from app.core import reports, security
from app.core.reports import get_report, save_report
from app.core.security import UserContext
from app.tools.registry import run_tool
from app.tools.hr import reports as report_tools

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


def test_report_creation_and_download_require_current_frappe_export_permission(monkeypatch):
    credentials = Credentials(authorization="token example:test")
    can_export = False
    checks = []
    builds = []

    class Frappe:
        def has_document_permission(self, doctype, permission):
            assert current_holder().credentials == credentials
            assert doctype == "Salary Slip"
            checks.append(permission)
            return permission == "read" or can_export

    def build_payslips(ctx, params):
        assert ctx == MGR
        builds.append(params)
        return "Payslips", [str(i) for i in range(7)], [[1, 2, "USD", 4, 5, 6, "Submitted"]]

    monkeypatch.setattr(reports, "get_frappe_client", Frappe)
    monkeypatch.setitem(report_tools.REPORTS, "payslips", build_payslips)
    args = {"report_type": "payslips", "file_format": "csv"}
    with use_credentials(credentials):
        assert "Export permission" in run_tool("create_report", args, MGR)["error"]
        assert not builds
        can_export = True
        file = run_tool("create_report", args, MGR)["result"]["file"]
        assert len(builds) == 1

    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 12345)})
    with pytest.raises(HTTPException) as missing_session:
        main.download_report(request, file["id"], MGR, None)
    assert missing_session.value.status_code == 403
    response = main.download_report(request, file["id"], MGR, credentials)
    assert response.status_code == 200
    assert response.path == get_report(file["id"], MGR.user_id).path
    can_export = False
    with pytest.raises(HTTPException) as denied:
        main.download_report(request, file["id"], MGR, credentials)
    assert denied.value.status_code == 403
    assert "Export permission" in denied.value.detail
    assert checks == ["read", "export"] * 4
