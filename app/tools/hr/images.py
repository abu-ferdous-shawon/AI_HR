import logging
from dataclasses import dataclass

from app.core.frappe_client import FrappeError, get_frappe_client
from app.core.media import save_picture
from app.core.security import ToolInputError, UserContext
from app.tools.registry import tool

log = logging.getLogger("hr_ai.images")


@dataclass(frozen=True)
class ImageSource:
    doctype: str
    field: str            # the field that holds the picture's file path
    scope: str            # who may see it, see below
    caption: str          # shown under the picture
    link_field: str = "employee"   # only used by "linked_to_employee"


# scope = "own_employee": the picture is on the caller's own Employee record
# scope = "own_user":     the picture is on the caller's own login (User record)
# scope = "company":      the picture is on the caller's Company (not personal)
# scope = "linked_to_employee": the doctype has a link field to Employee, and
#                         the newest record of the CALLER's own is used
IMAGE_SOURCES = {
    "employee_photo": ImageSource("Employee", "image", "own_employee", "Your employee photo"),
    "profile_picture": ImageSource("User", "user_image", "own_user", "Your profile picture"),
    "company_logo": ImageSource("Company", "company_logo", "company", "Company logo"),
    # Example of adding another doctype that belongs to an employee:
    # "id_card_photo": ImageSource("Employee ID Card", "photo", "linked_to_employee", "Your ID card photo"),
}

EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


def _find_file_path(ctx: UserContext, src: ImageSource):
    """Find the stored file path. Every branch starts from the caller's own ID."""
    frappe = get_frappe_client()
    if src.scope == "own_employee":
        rows = frappe.get_list(src.doctype, ["*"], filters={"name": ctx.employee_id}, limit=1)
        return rows[0].get(src.field) if rows else None
    if src.scope == "company":
        rows = frappe.get_list("Employee", ["*"], filters={"name": ctx.employee_id}, limit=1)
        company = rows[0].get("company") if rows else None
        return frappe.get_doc(src.doctype, company).get(src.field) if company else None

    if src.scope == "own_user":
        rows = frappe.get_list("Employee", ["*"], filters={"name": ctx.employee_id}, limit=1)
        login = rows[0].get("user_id") if rows else None
        if not login:
            return None
        found = frappe.get_list(src.doctype, [src.field], filters={"name": login}, limit=1)
        return found[0].get(src.field) if found else None
    
    if src.scope == "linked_to_employee":
        rows = frappe.get_list(
            src.doctype, ["*"],
            filters=[[src.link_field, "=", ctx.employee_id], [src.field, "is", "set"]],
            order_by="modified desc", limit=1)
        return rows[0].get(src.field) if rows else None
    raise ValueError(f"unknown scope {src.scope}")


@tool(
    name="show_image",
    description=(
        "Show a picture stored in the HR system to the person asking: their own "
        "employee photo (employee_photo), their own login profile picture "
        "(profile_picture) or the company logo (company_logo). Use employee_photo "
        "when they just say 'my photo'. The picture appears in the chat under "
        "your message. You cannot see it yourself."
    ),
    parameters={"type": "object", "properties": {
        "source": {"type": "string", "enum": list(IMAGE_SOURCES),
                   "description": "Which picture to show."},
    }, "required": ["source"]},
)
def show_image(ctx: UserContext, source: str | None = None) -> dict:
    src = IMAGE_SOURCES.get(source or "")
    if src is None:
        raise ToolInputError(f"source must be one of: {', '.join(IMAGE_SOURCES)}.")

    file_path = _find_file_path(ctx, src)
    if not file_path or not str(file_path).strip():
        return {"message": f"There is no picture on record for this ({source})."}

    try:
        data, mime = get_frappe_client().get_file(str(file_path))
    except FrappeError as exc:
        log.warning("Could not load %s for %s: %s", source, ctx.user_id, exc)
        return {"message": "A picture is on record, but it could not be loaded."}

    picture = save_picture(ctx.user_id, f"{source}.{EXTENSIONS[mime]}", data, mime)
    return {"image": {"id": picture["id"], "name": picture["name"], "caption": src.caption},
            "message": f"The picture is shown below ({src.caption})."}