from app.config import settings
from app.core.frappe_client import FrappeError, get_frappe_client

# 1. Secrets must be masked when printed.
print("Settings loaded:", settings.frappe_url, settings.frappe_api_key)

frappe = get_frappe_client()

# 2. Which user does Frappe think we are?
print("Logged in as:", frappe.call_method("frappe.auth.get_logged_user"))

# 3. List a few employees (only two safe fields).
employees = frappe.get_list("Employee", ["name", "employee_name"], limit=3)
print("Employees:", employees)

# 4. Fetch one full record and show only a few fields.
if employees:
    doc = frappe.get_doc("Employee", employees[0]["name"])
    print("First employee:", doc.get("name"), "-", doc.get("employee_name"))

# 5. A missing record must raise our own error, not crash.
try:
    frappe.get_doc("Employee", "DOES-NOT-EXIST")
except FrappeError as err:
    print("Expected error caught:", err.status_code)