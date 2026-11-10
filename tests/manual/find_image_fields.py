import sys

from app.core.frappe_client import FrappeError, get_frappe_client

# Doctypes likely to carry pictures. Pass your own names to check others, e.g.
#   python -m tests.manual.find_image_fields "Employee ID Card" "Visitor"
DEFAULTS = ["Employee", "Company", "User", "Job Applicant", "Employee Referral",
            "Employee Onboarding", "Employee Separation", "Expense Claim",
            "Branch", "Department", "Designation"]
IMAGE_WORDS = ("image", "photo", "picture", "logo", "signature", "avatar", "selfie")

names = sys.argv[1:] or DEFAULTS
frappe = get_frappe_client()
rows = []

for doctype in names:
    try:
        body = frappe._get("/api/method/frappe.desk.form.load.getdoctype",
                           {"doctype": doctype})
    except FrappeError as err:
        print(f"{doctype}: not available to this user ({err.status_code})")
        continue
    docs = body.get("docs") or (body.get("message") or {}).get("docs") or []
    if not docs:
        print(f"{doctype}: could not read its fields")
        continue
    for doc in docs:   # the doctype itself, plus its child tables
        for field in doc.get("fields", []):
            kind, name = field.get("fieldtype"), field.get("fieldname") or ""
            if kind == "Attach Image" or (kind == "Attach" and any(w in name for w in IMAGE_WORDS)):
                rows.append((doc.get("name"), name, field.get("label") or "", kind))

print(f"\n{'DocType':32} {'Field':22} {'Label':28} Type")
for doctype, name, label, kind in sorted(set(rows)):
    print(f"{doctype:32} {name:22} {label:28} {kind}")
if not rows:
    print("(no picture fields found in the doctypes checked)")