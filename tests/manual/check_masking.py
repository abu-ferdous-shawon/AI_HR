import sys

from app.core.frappe_client import get_frappe_client
from app.core.masking import is_confidential
from app.tools.hr.profile import HIDDEN_FIELDS, is_empty

emp = sys.argv[1] if len(sys.argv) > 1 else "HR-EMP-00001"
row = get_frappe_client().get_list("Employee", ["*"], filters={"name": emp}, limit=1)[0]

groups = {"HIDDEN (technical)": [], "MASKED (confidential)": [], "SHOWN": []}
for name, value in sorted(row.items()):
    label = f"{name} ({'empty' if is_empty(value) else 'filled'})"
    if name in HIDDEN_FIELDS or name.startswith("_"):
        groups["HIDDEN (technical)"].append(label)
    elif is_confidential(name):
        groups["MASKED (confidential)"].append(label)
    else:
        groups["SHOWN"].append(label)

for title, names in groups.items():
    print(f"\n== {title} ==")
    print("\n".join("  " + n for n in names) or "  (none)")