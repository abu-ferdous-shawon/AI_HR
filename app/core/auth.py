import requests

from app.config import settings
from app.core.frappe_client import FrappeError
from app.core.security import AccessDenied


def identify_user(authorization: str | None, cookie: str | None) -> str:
    """Ask Frappe who these credentials belong to. Returns the user's login."""
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    elif cookie:
        headers["Cookie"] = cookie
    else:
        raise AccessDenied("Please sign in first.")

    url = settings.frappe_url.rstrip("/") + "/api/method/frappe.auth.get_logged_user"
    try:
        response = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as exc:
        raise FrappeError(0, f"could not reach Frappe: {exc}") from exc

    if response.status_code >= 500:
        raise FrappeError(response.status_code, "login check failed")
    if response.status_code >= 400:
        raise AccessDenied("Your sign-in is not valid.")

    try:
        user = response.json().get("message")
    except ValueError:
        user = None
    if not user or user == "Guest":
        raise AccessDenied("Your sign-in is not valid.")
    return user