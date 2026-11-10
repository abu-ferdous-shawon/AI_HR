"""Run from the project root:  python probe_meta.py
Prints structure only (field names/types, select options, row counts). No personal values. Writes probe_meta.txt"""
from app.core.frappe_client import get_frappe_client

DOCTYPES = [
    "Attendance", "Attendance Request", "Employee Checkin", "Leave Application", "Leave Allocation",
    "Leave Policy Assignment", "Holiday List Assignment", "Employee Onboarding", "Employee Transfer",
    "Employee Promotion", "Employee Grievance", "Employee Separation", "Exit Interview",
    "Full and Final Statement", "Salary Withholding", "Shift Request", "Shift Assignment", "Expense Claim",
    "Employee Benefit Application", "Employee Benefit Claim", "Salary Structure Assignment", "Salary Slip",
    "Additional Salary", "Timesheet", "Employee Incentive", "Retention Bonus", "Overtime Slip", "Arrear",
    "Payroll Correction", "Training Event", "Training Result", "Training Feedback", "Employee Skill Map",
    "Appraisal", "Contract",
]
SKIP = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold", "Image"}
DATES = {"Date", "Datetime"}
c = get_frappe_client()
out = []

for dt in DOCTYPES:
    try:
        res = c.call_method("frappe.desk.form.load.getdoctype", {"doctype": dt, "with_parent": 0})
        docs = res.get("docs") if isinstance(res, dict) else None
        meta = next((d for d in docs or [] if d.get("name") == dt), None)
        if not meta:
            out.append(f"## {dt}\n  structure: not available"); continue
    except Exception as e:
        out.append(f"## {dt}\n  structure: FAILED status={getattr(e, 'status_code', '?')}"); continue
    try:
        n = c.call_method("frappe.client.get_count", {"doctype": dt})
    except Exception as e:
        n = f"FAILED {getattr(e, 'status_code', '?')}"
    fields = [f for f in meta.get("fields", []) if f.get("fieldtype") not in SKIP]
    emp = [f["fieldname"] for f in fields if f.get("fieldtype") == "Link" and f.get("options") == "Employee"]
    dates = [f["fieldname"] for f in fields if f.get("fieldtype") in DATES]
    sels = {f["fieldname"]: (f.get("options") or "").replace("\n", "|") for f in fields
            if f.get("fieldtype") == "Select" and f["fieldname"] in ("status", "approval_status", "docstatus", "type")}
    tables = {f["fieldname"]: f.get("options") for f in fields if f.get("fieldtype") == "Table"}
    out.append(f"## {dt}  rows={n}  submittable={meta.get('is_submittable')}\n  employee_field={emp}  dates={dates}\n"
               f"  selects={sels}  child_tables={tables}\n  fields=" +
               ",".join(f"{f['fieldname']}:{f.get('fieldtype')}" for f in fields))

text = "\n".join(out)
open("probe_meta.txt", "w", encoding="utf-8").write(text)
print(text[:1500], "\n...\nSaved probe_meta.txt - open it, check it, then attach it to the chat.")