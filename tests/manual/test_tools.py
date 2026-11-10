from app.core.frappe_client import get_frappe_client
from app.core.security import UserContext, get_team, resolve_context
from app.tools.registry import get_tool_specs, load_all_tools, run_tool

load_all_tools()
frappe = get_frappe_client()


def check(label, ok, detail=""):
    print(f"{label}: {'PASS' if ok else 'FAIL'} {detail}")


# ---- Part 1: do the tools return correct data? ----
# This context is built by hand so we can test an employee that has data.
emp = UserContext(user_id="test-only", employee_id="HR-EMP-00001")

res = run_tool("get_my_leave_balance", {}, emp)
got = {b["leave_type"]: b["remaining"] for b in res.get("result", {}).get("balances", [])}
check("1 leave balance", got == {"Annual Leave": 15.0, "Casual Leave": 15.0, "Sick Leave": 10.0}, got)

res = run_tool("get_attendance", {"from_date": "2026-01-25", "to_date": "2026-01-29"}, emp)
summary = res.get("result", {}).get("summary")
check("2 own attendance", summary == {"Present": 4, "Absent": 1}, summary)

for label, args in [
    ("3a reversed dates", {"from_date": "2026-02-01", "to_date": "2026-01-01"}),
    ("3b range too long", {"from_date": "2025-01-01", "to_date": "2026-01-01"}),
    ("3c not a date", {"from_date": "yesterday"}),
]:
    res = run_tool("get_attendance", args, emp)
    check(label + " refused with a clear message", "error" in res, res.get("error"))

# ---- Part 2: can the real manager login be abused? ----
manager = resolve_context("test@mail.com")
team = get_team(manager)
team_ids = {m["name"] for m in team}
outsiders = [e["name"] for e in frappe.get_list("Employee", ["name"], limit=50)
             if e["name"] != manager.employee_id and e["name"] not in team_ids]
print("\nManager:", manager.employee_id, "| team:", sorted(team_ids), "| outsiders:", outsiders)

# 4. Every tool that accepts an employee_id must refuse an outsider.
for spec in get_tool_specs():
    if "employee_id" in spec["parameters"].get("properties", {}):
        res = run_tool(spec["name"], {"employee_id": outsiders[0]}, manager)
        check(f"4 {spec['name']} refuses outsider {outsiders[0]}", "error" in res, res.get("error"))

# 5. A direct report's attendance is allowed.
if team:
    res = run_tool("get_attendance", {"employee_id": team[0]["name"],
                                      "from_date": "2026-01-25", "to_date": "2026-01-29"}, manager)
    check(f"5 manager reads {team[0]['name']}'s attendance", "result" in res, res.get("error"))
else:
    print("5 skipped: no direct reports. Do Step 1 first.")