from app.core.frappe_client import FrappeError, get_frappe_client
from app.core.security import AccessDenied, UserContext
from app.tools.registry import tool

# Technical fields nobody needs in a chat. Add a field name here to hide it
# completely, even from the person it belongs to.
HIDDEN_FIELDS = {
    "owner", "creation", "modified", "modified_by", "docstatus", "idx",
    "naming_series", "doctype", "lft", "rgt", "old_parent", "image",
    "create_user", "create_user_permission", "unsubscribed",
}

# Housekeeping columns that every row of a table inside the form carries
# (education, work history ...). They mean nothing to the person.
ROW_SYSTEM_KEYS = {
    "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx",
    "parent", "parentfield", "parenttype", "doctype",
}


def is_empty(value) -> bool:
    """Frappe stores 'not filled in' as None, '' or 0, so all count as empty."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (list, dict)):
        return not value
    return False


def clean_rows(rows: list) -> list:
    """Keep only the meaningful cells of each row of a table inside the form."""
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()
                if k not in ROW_SYSTEM_KEYS and not k.startswith("_") and not is_empty(v)}
        if item:
            cleaned.append(item)
    return cleaned


@tool(
    name="get_my_profile",
    description=(
        "Get the logged-in person's own complete profile: work details, "
        "personal and contact details, address, emergency contact, education "
        "and qualifications, past work history, ID and bank details. Returns "
        "'details' (fields that have a value) and 'no_information_on_record' "
        "(fields that are empty). Only works for the person asking, never for "
        "anyone else."
    ),
    allow=("*",),  # confidential values are masked by the agent, not removed here
)
def get_my_profile(ctx: UserContext) -> dict:
    # No parameters: the ID always comes from ctx, never from the model.
    try:
        doc = get_frappe_client().get_doc("Employee", ctx.employee_id)
    except FrappeError as exc:
        if exc.status_code == 404:
            raise AccessDenied("Your employee record could not be found.")
        raise
    if not doc:
        raise AccessDenied("Your employee record could not be found.")

    details: dict = {}
    missing: list[str] = []
    for field_name, value in sorted(doc.items()):
        if field_name in HIDDEN_FIELDS or field_name.startswith("_"):
            continue
        if isinstance(value, list):          # a table inside the form, e.g. education
            value = clean_rows(value)
        if is_empty(value):
            missing.append(field_name)
        else:
            details[field_name] = value.strip() if isinstance(value, str) else value
    return {"details": details, "no_information_on_record": missing}