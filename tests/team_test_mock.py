from unittest.mock import patch

from app.core.security import UserContext
from app.tools.registry import load_all_tools, run_tool

load_all_tools()


def check(label, ok, detail=""):
    print(f"{label}: {'PASS' if ok else 'FAIL'} {detail}")


manager = UserContext(user_id="mgr-test", employee_id="HR-EMP-00004")
fake_team = [{"name": "HR-EMP-00001", "employee_name": "Employee 2"}]
dates = {"from_date": "2026-01-25", "to_date": "2026-01-29"}

# Pretend the manager has exactly one direct report: HR-EMP-00001.
# (get_team is looked up by name in two modules, so both are patched.)
with patch("app.core.security.get_team", return_value=fake_team), \
     patch("app.tools.hr.team.get_team", return_value=fake_team):

    res = run_tool("get_attendance", {"employee_id": "HR-EMP-00001", **dates}, manager)
    check("1 manager reads a direct report's attendance",
          res.get("result", {}).get("summary") == {"Present": 4, "Absent": 1}, res)

    res = run_tool("get_attendance", {"employee_id": "HR-EMP-00002", **dates}, manager)
    check("2 manager refused for a non-report", "error" in res, res.get("error"))

    res = run_tool("list_my_team", {}, manager)
    check("3 list_my_team shows the team",
          [m["name"] for m in res.get("result", [])] == ["HR-EMP-00001"], res)

    res = run_tool("get_my_personal_info", {"employee_id": "HR-EMP-00001"}, manager)
    check("4 manager does NOT get a report's personal info",
          res.get("result", {}).get("employee_name", "").strip() != "Employee 2", res)