import sys

from app.core.frappe_client import get_frappe_client

emp = sys.argv[1] if len(sys.argv) > 1 else "HR-EMP-00001"
rows = get_frappe_client().get_list("Employee", ["*"], filters={"name": emp}, limit=1)
for key, value in rows[0].items():
    print(f"{key}: {'(filled)' if value not in (None, '', 0) else '-'}")