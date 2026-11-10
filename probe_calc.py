"""Run from the project root:  python probe_calc.py hr.login@company.com [other@company.com ...]
Prints ONLY field names, counts and settings (no personal values) to probe_calc.txt."""
import sys
from app.core.frappe_client import get_frappe_client

out = []
def say(*a): out.append(" ".join(str(x) for x in a))
def err(e): return f"FAILED status={getattr(e, 'status_code', '?')} {type(e).__name__}"

c = get_frappe_client()

def keys_of(doctype, filters=None):
    try:
        rows = c.get_list(doctype, ["*"], filters=filters or {}, limit=1)
        say(f"[{doctype}] fields:", sorted(rows[0].keys()) if rows else "no rows")
        return rows[0].get("name") if rows else None
    except Exception as e:
        say(f"[{doctype}] fields:", err(e))

name = keys_of("Expense Claim")
if name:
    try:
        doc = c.get_doc("Expense Claim", name)
        rows = doc.get("expenses") or []
        say("[Expense Claim] child 'expenses' row fields:", sorted(rows[0].keys()) if rows else "none")
    except Exception as e:
        say("[Expense Claim] child:", err(e))
keys_of("Salary Slip")

say("\n[Leave Type] rule fields per type (config, not personal):")
try:
    for r in c.get_all("Leave Type", ["name", "allow_negative", "max_continuous_days_allowed", "include_holiday", "is_lwp"], max_rows=100):
        say("  ", r)
except Exception as e:
    say("  ", err(e))

say("\nRow counts:")
for dt in ["Expense Claim", "Salary Slip", "GL Entry", "Purchase Invoice", "Journal Entry"]:
    try:
        say(f"  {dt}:", c.call_method("frappe.client.get_count", {"doctype": dt}))
    except Exception as e:
        say(f"  {dt}:", err(e))

say("\nStatus values in use (counts only):")
for dt, field in [("Expense Claim", "status"), ("Expense Claim", "approval_status"), ("Salary Slip", "status")]:
    try:
        seen = {}
        for r in c.get_all(dt, [field], max_rows=2000):
            seen[r.get(field)] = seen.get(r.get(field), 0) + 1
        say(f"  {dt}.{field}:", seen)
    except Exception as e:
        say(f"  {dt}.{field}:", err(e))

say("\nAggregate query support:")
try:
    r = c.get_list("Expense Claim", ["sum(total_claimed_amount) as total", "count(name) as n"], limit=1)
    say("  sum() works; keys:", sorted(r[0].keys()) if r else "empty")
except Exception as e:
    say("  sum() in get_list:", err(e))

say("\nRoles of each login given:")
for login in sys.argv[1:]:
    try:
        roles = [r.get("role") for r in c.get_doc("User", login).get("roles", [])]
        say(f"  {login}:", sorted(roles))
    except Exception as e:
        say(f"  {login}:", err(e))

open("probe_calc.txt", "w", encoding="utf-8").write("\n".join(out))
print("\n".join(out))
print("\nSaved probe_calc.txt - open it, check it, then attach it to the chat.")