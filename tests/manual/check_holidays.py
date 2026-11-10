import datetime
import sys

from app.core.frappe_client import FrappeError, get_frappe_client

emp = sys.argv[1] if len(sys.argv) > 1 else "HR-EMP-00001"
frappe = get_frappe_client()
row = frappe.get_list("Employee", ["*"], filters={"name": emp}, limit=1)[0]

print("Employee's own holiday list :", row.get("holiday_list") or "(none)")
print("Employee's company          :", row.get("company"))
company_default = None
if row.get("company"):
    try:
        company_default = frappe.get_doc("Company", row["company"]).get("default_holiday_list")
    except FrappeError as err:
        print("Could not read the company:", err)
print("Company default holiday list:", company_default or "(none)")

name = row.get("holiday_list") or company_default
if not name:
    sys.exit("\nNo holiday list applies to this employee. That is why holidays are missing.")

entries = frappe.get_doc("Holiday List", name).get("holidays") or []
public = [h for h in entries if not h.get("weekly_off")]
weekly = [h for h in entries if h.get("weekly_off")]
day_name = lambda h: datetime.date.fromisoformat(str(h["holiday_date"])).strftime("%A")
print(f"\nUsing '{name}': {len(entries)} entries = {len(public)} public holidays + {len(weekly)} weekly offs")
print("Weekly off days:", sorted({day_name(h) for h in weekly}) or "none")
today = datetime.date.today().isoformat()
upcoming = [h for h in public if str(h["holiday_date"]) >= today]
print("Upcoming public holidays:",
      [(str(h["holiday_date"]), (h.get("description") or "").strip()) for h in upcoming] or "none")