import sys

from app.core.frappe_client import get_frappe_client
from app.core.security import AccessDenied, resolve_context, scrub
from app.tools.registry import get_tool_specs, load_all_tools, run_tool

load_all_tools()
print("Registered tools:", [t["name"] for t in get_tool_specs()])

frappe = get_frappe_client()
linked = frappe.get_list(
    "Employee", ["name", "user_id"], filters=[["user_id", "is", "set"]], limit=2
)
if not linked:
    print("No employee has a User ID set. In Frappe, open an Employee and fill "
          "the 'User ID' field, then run this again.")
    sys.exit(1)

me = resolve_context(linked[0]["user_id"])
print("\nContext:", me)
print("Own profile:", run_tool("get_my_profile", {}, me))

# Attack 1: pretend to be someone else by passing another employee's ID.
others = [e["name"] for e in frappe.get_list("Employee", ["name"], limit=5)
          if e["name"] != me.employee_id]
result = run_tool("get_my_profile", {"employee_id": others[0]}, me)
ok = result.get("result", {}).get("name") == me.employee_id
print(f"\nAttack 1 (ask for {others[0]}):", "PASS - got my own data" if ok else "FAIL")

# Attack 2: call a tool that does not exist.
result = run_tool("run_sql", {"query": "SELECT * FROM tabEmployee"}, me)
print("Attack 2 (unknown tool):", "PASS" if "error" in result else "FAIL", result)

# Attack 3: a login with no employee record.
try:
    resolve_context("nobody@example.com")
    print("Attack 3 (unlinked login): FAIL")
except AccessDenied as err:
    print("Attack 3 (unlinked login): PASS -", err)

# Attack 4: sensitive fields must be stripped.
cleaned = scrub({"name": "X", "bank_ac_no": "123456", "nested": {"iban": "BD00"}})
print("Attack 4 (scrub):", "PASS" if cleaned == {"name": "X", "nested": {}} else "FAIL", cleaned)