import sys

from app.core.frappe_client import get_frappe_client
from app.core.security import AccessDenied, get_team, require_access, resolve_context
from app.tools.registry import load_all_tools, run_tool

load_all_tools()
frappe = get_frappe_client()

manager_login = sys.argv[1] if len(sys.argv) > 1 else "test@mail.com"
manager = resolve_context(manager_login)
team = get_team(manager)
print("Manager:", manager.employee_id, "| team:", team)
if not team:
    sys.exit("This person has no direct reports. Do Step 3 first.")

team_ids = {m["name"] for m in team}
outsiders = [
    e["name"] for e in frappe.get_list("Employee", ["name"], limit=50)
    if e["name"] != manager.employee_id and e["name"] not in team_ids
]


def check(label, passed):
    print(f"{label}: {'PASS' if passed else 'FAIL'}")


# 1. Manager may use own ID, or none at all.
check("1 own ID allowed",
      require_access(manager, None) == manager.employee_id
      and require_access(manager, manager.employee_id) == manager.employee_id)

# 2. Manager may use a direct report's ID.
member = team[0]["name"]
check("2 team member allowed", require_access(manager, member) == member)

# 3. Manager may NOT use an outsider's ID.
if outsiders:
    try:
        require_access(manager, outsiders[0])
        check(f"3 outsider {outsiders[0]} refused", False)
    except AccessDenied:
        check(f"3 outsider {outsiders[0]} refused", True)
else:
    print("3 skipped: no employee outside the team to test with")

# 4. The team tool returns exactly the team.
result = run_tool("list_my_team", {}, manager)
check("4 list_my_team", {m["name"] for m in result.get("result", [])} == team_ids)

# 5. The reverse: a team member must NOT see their manager.
login = frappe.get_doc("Employee", member).get("user_id")
if login:
    member_ctx = resolve_context(login)
    try:
        require_access(member_ctx, manager.employee_id)
        check("5 report cannot access manager", False)
    except AccessDenied:
        check("5 report cannot access manager", True)
else:
    print(f"5 skipped: {member} has no User ID, so no login to test as")