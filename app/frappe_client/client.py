import json
import re
from functools import lru_cache
from typing import Any
from urllib.parse import quote, unquote, urlparse
import logging

from app.frappe_client.service_purpose import active_purpose
import requests

from app.core.sites import current_site, get_sites
from app.auth.credentials import Credentials, current_holder, service_account_chosen
from app.core.media import sniff_image
from app.knowledge_packs.redactor import NeverSendPolicy, current_policy

log = logging.getLogger("hr_ai.service")

# Safety cap: no single request may pull more than this many records.
MAX_LIMIT = 200
MAX_FILE_BYTES = 5 * 1024 * 1024   # largest picture we will download
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
ORDER_FIELD = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)(?:\s+(?:asc|desc))?", re.IGNORECASE)
VALIDATION_ERRORS = frozenset({"ValidationError", "MandatoryError", "DataError", "InvalidFilterOperator",
                               "InvalidFieldNameError", "InvalidDateError"})
DATABASE_ERRORS = frozenset({"ProgrammingError", "OperationalError", "UndefinedColumn"})
STANDARD_COLUMNS = ("name", "owner", "creation", "modified", "modified_by", "docstatus", "idx")
DISPLAY_FIELD_TYPES = frozenset({
    "Section Break", "Column Break", "Tab Break", "HTML", "Fold", "Heading",
    "Button", "Image", "Attachment Gallery", "Vue Component",
})
NON_COLUMN_TYPES = DISPLAY_FIELD_TYPES | {"Table", "Table MultiSelect", "Password"}


class FrappeError(Exception):
    """Raised when Frappe cannot be reached or returns an error."""

    def __init__(self, status_code: int, message: str, *, error_type: str = "",
                 details: tuple[str, ...] = ()):
        super().__init__(f"Frappe error {status_code}: {message}")
        self.status_code = status_code  # 0 means we never got a reply
        self.message = message
        self.error_type = error_type
        self.details = details  # Untrusted text for validation diagnostics only. Never log it.


def _read_error(response: requests.Response) -> FrappeError:
    status = response.status_code
    body = {}
    if len(response.content) <= 32_768:
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            pass
    kind = body.get("exc_type")
    if not isinstance(kind, str) or len(kind) > 64 or not FIELD_NAME.fullmatch(kind):
        kind = ""
    details = []
    if status in (400, 417, 422) or (status == 500 and kind in DATABASE_ERRORS):
        details.extend(body.get(key) for key in ("_error_message", "message", "exception"))
        try:
            messages = json.loads(body.get("_server_messages") or "[]")
        except (TypeError, ValueError):
            messages = []
        if isinstance(messages, list):
            for item in messages[:8]:
                try:
                    entry = json.loads(item)
                except (TypeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    details.append(entry.get("message"))
    return FrappeError(status, "request failed", error_type=kind,
                       details=tuple(item for item in details if isinstance(item, str) and 0 < len(item) <= 400))

class UserLoginRequired(FrappeError):
    """A data read had no user login and did not choose the service account."""

    def __init__(self):
        super().__init__(401, "no user login is attached to this request")

class ServicePurposeRequired(FrappeError):
    """The service account was used without saying why."""

    def __init__(self):
        super().__init__(403, "the service account was used without a declared purpose")


# Off by default so scripts and tests behave as before. The web server switches it on.
_strict_user_login = False


def enforce_user_login(on: bool = True) -> None:
    """When on, get_frappe_client() refuses to fall back to the integration key."""
    global _strict_user_login
    _strict_user_login = on

class FrappeClient:
    """Small helper for Frappe's REST API. It knows nothing about HR.

    With credentials it acts as that person, so the ERP applies their roles and
    permissions. Without them it uses the integration account's API key.
    """

    def __init__(self, base_url: str | None = None,
                 api_key: str | None = None,
                 api_secret: str | None = None,
                 timeout: int = 30,
                 credentials: Credentials | None = None):
        self.base_url = (base_url or current_site().frappe_url).rstrip("/")
        self.timeout = timeout
        self.acts_as_user = credentials is not None

        # A Session reuses the connection and sends the same headers each time.
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if credentials is not None:
            self.session.headers.update(credentials.headers())
        else:
            key = api_key or current_site().api_key.get_secret_value()
            secret = api_secret or current_site().api_secret.get_secret_value()
            self.session.headers["Authorization"] = f"token {key}:{secret}"

    # ---------- internal helpers ----------

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        try:
            response = self.session.get(url, params=params, timeout=self.timeout,
                                        allow_redirects=False)
        except requests.RequestException:
            raise FrappeError(0, "could not reach Frappe") from None

        if 300 <= response.status_code < 400:   # usually the login page: the session ended
            raise FrappeError(401, "the HR system redirected the request "
                                   "(the login may have expired)")
        if response.status_code >= 400:
            raise _read_error(response)

        try:
            return response.json()
        except ValueError as exc:
            raise FrappeError(response.status_code, "reply was not valid JSON") from exc

    def _list_fields(self, doctype: str, fields: list[str] | None,
                     policy: NeverSendPolicy) -> list[str]:
        requested = list(fields or ["name"])
        if "*" in requested:
            meta = next((doc for doc in self.get_meta(doctype) if doc.get("name") == doctype), None)
            if meta is None:
                raise FrappeError(403, "could not resolve a safe field list")
            requested = [field for field in requested if field != "*"] + list(STANDARD_COLUMNS)
            requested.extend(
                row["fieldname"] for row in meta.get("fields", [])
                if row.get("fieldname") and row.get("fieldtype") not in NON_COLUMN_TYPES
                and row.get("is_virtual") not in (True, 1, "1")
            )
        if any(not isinstance(field, str) or not FIELD_NAME.fullmatch(field) for field in requested):
            raise FrappeError(400, "fields must be simple field names")
        projected = policy.project(list(dict.fromkeys(requested)), doctype)
        if not projected:
            raise FrappeError(403, "no requested fields are available")
        return projected

    @staticmethod
    def _check_query_fields(policy, doctype, filters, order_by) -> None:
        if isinstance(filters, dict):
            columns = [(doctype, name) for name in filters]
        else:
            columns = [(part[0], part[1]) if len(part) == 4 else (doctype, part[0])
                       for part in (filters or []) if isinstance(part, (list, tuple)) and len(part) in (3, 4)]
        if order_by:
            for term in order_by.split(","):
                match = ORDER_FIELD.fullmatch(term.strip())
                if not match:
                    raise FrappeError(400, "order_by must contain simple field names")
                columns.append((doctype, match[1]))
        for source, name in columns:
            if not isinstance(source, str) or not isinstance(name, str) or not FIELD_NAME.fullmatch(name):
                raise FrappeError(400, "filters must use simple field names")
            if policy.blocked(name, source):
                raise FrappeError(403, "a requested filter or sort field is unavailable")

    # ---------- public methods ----------

    def get_list(self, doctype: str, fields: list[str] | None = None,
                 filters: dict | list | None = None, order_by: str | None = None,
                 limit: int = 20, start: int = 0, *,
                 or_filters: dict | list | None = None) -> list[dict[str, Any]]:
        """Fetch several records, e.g. get_list('Leave Type', ['name'])."""
        policy = current_policy()
        self._check_query_fields(policy, doctype, filters, order_by)
        self._check_query_fields(policy, doctype, or_filters, None)
        projection = self._list_fields(doctype, fields, policy)
        params: dict[str, Any] = {
            "fields": json.dumps(projection),
            "limit_page_length": min(limit, MAX_LIMIT),
            "limit_start": max(start, 0),
        }
        if filters:
            params["filters"] = json.dumps(filters)
        if or_filters:
            params["or_filters"] = json.dumps(or_filters)
        if order_by:
            params["order_by"] = order_by

        body = self._get(f"/api/resource/{quote(doctype)}", params)
        return policy.sanitize(body.get("data", []), doctype)

    def get_all(self, doctype: str, fields: list[str] | None = None,
                filters: dict | list | None = None, order_by: str | None = None,
                max_rows: int = 2000) -> list[dict[str, Any]]:
        """Like get_list, but reads page after page up to max_rows."""
        if fields and "*" in fields:
            fields = self._list_fields(doctype, fields, current_policy())
        rows: list[dict[str, Any]] = []
        while len(rows) < max_rows:
            page = self.get_list(doctype, fields, filters, order_by,
                                 limit=MAX_LIMIT, start=len(rows))
            rows.extend(page)
            if len(page) < MAX_LIMIT:
                break
        return rows[:max_rows]

    def get_doc(self, doctype: str, name: str, fields: list[str] | None = None) -> dict[str, Any]:
        """Read a permitted document, with optional local field projection."""
        policy = current_policy()
        include = set(policy.project(fields, doctype)) if fields is not None else None
        if include is not None and not include:
            return {}
        path = f"/api/resource/{quote(doctype)}/{quote(name, safe='')}"
        # Frappe's document endpoint has no field projection and includes child rows.
        doc = self._get(path).get("data", {})
        if include is not None:
            doc = {field: value for field, value in doc.items() if field in include}
        return policy.sanitize(doc, doctype, document=True)

    def get_aggregate(self, doctype: str, function: str, aggregate_field: str, *,
                      group_by: str | None = None, filters: list | None = None,
                      limit: int = 51) -> list[dict]:
        """Use Frappe's structured ORM functions with the same permission checks as get_list."""
        if function not in {"count", "sum", "avg", "min", "max"}:
            raise FrappeError(400, "unsupported aggregation")
        policy = current_policy()
        columns = [aggregate_field] + ([group_by] if group_by else [])
        if any(not FIELD_NAME.fullmatch(column) for column in columns):
            raise FrappeError(400, "aggregation fields must be simple field names")
        if policy.project(columns, doctype) != columns:
            raise FrappeError(403, "an aggregation field is unavailable")
        self._check_query_fields(policy, doctype, filters, group_by)
        fields = ([group_by] if group_by else []) + [{function.upper(): aggregate_field, "as": "_metric"}]
        params: dict[str, Any] = {
            "fields": json.dumps(fields), "limit_page_length": min(limit, MAX_LIMIT),
        }
        if filters:
            params["filters"] = json.dumps(filters)
        if group_by:
            params.update(group_by=group_by, order_by=f"{group_by} asc")
        rows = self._get(f"/api/resource/{quote(doctype)}", params).get("data", [])
        return policy.sanitize(rows, doctype)

    def call_method(self, method_path: str, params: dict | None = None) -> Any:
        """Call a whitelisted Frappe function, e.g. frappe.auth.get_logged_user."""
        policy = current_policy()
        value = self._get(f"/api/method/{method_path}", params).get("message")
        if method_path == "frappe.desk.query_report.run":
            return policy.sanitize_report(value)
        return policy.sanitize(value)

    def has_document_permission(self, doctype: str, permission: str, name: str | None = None) -> bool:
        result = self.call_method("frappe.client.has_permission", {
            "doctype": doctype, "docname": name or "", "perm_type": permission,
        })
        return isinstance(result, dict) and result.get("has_permission") in (True, 1)

    def _write_document(self, method: str, doctype: str, name: str | None,
                        payload: dict | None = None) -> dict:
        if not self.acts_as_user:
            raise UserLoginRequired()
        path = f"/api/resource/{quote(doctype, safe='')}"
        if name is not None:
            if name in {"", ".", ".."} or "/" in name or "\\" in name:
                raise FrappeError(400, "invalid record ID")
            path += f"/{quote(name, safe='')}"
        try:
            response = self.session.request(
                method, self.base_url + path, json=payload, timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise FrappeError(0, "could not verify the write outcome") from None
        if not 200 <= response.status_code < 300:
            error_type = ""
            try:
                body = response.json()
                candidate = body.get("exc_type") if isinstance(body, dict) else None
                if isinstance(candidate, str) and FIELD_NAME.fullmatch(candidate):
                    error_type = candidate
            except ValueError:
                pass
            code = 401 if 300 <= response.status_code < 400 else response.status_code
            raise FrappeError(code, "write request failed", error_type=error_type)
        if method == "DELETE":
            return {}
        try:
            document = response.json()["data"]
        except (ValueError, TypeError, KeyError):
            raise FrappeError(0, "could not verify the write outcome") from None
        if not isinstance(document, dict):
            raise FrappeError(0, "could not verify the write outcome")
        return current_policy().sanitize(document, doctype, document=True)

    def create_document(self, doctype: str, payload: dict) -> dict:
        return self._write_document("POST", doctype, None, payload)

    def update_document(self, doctype: str, name: str, payload: dict, *, modified: str) -> dict:
        return self._write_document("PUT", doctype, name, {**payload, "modified": modified})

    def delete_document(self, doctype: str, name: str) -> None:
        self._write_document("DELETE", doctype, name)

    def get_meta(self, doctype: str) -> list[dict[str, Any]]:
        """The form's own field list for a doctype and its child tables.

        The answer sits under 'docs', not 'message', which is why call_method cannot
        read it. Returns [] when the ERP answers in another shape.
        """
        body = self._get("/api/method/frappe.desk.form.load.getdoctype", {"doctype": doctype})
        docs = body.get("docs")
        message = body.get("message")
        if not docs and isinstance(message, dict):
            docs = message.get("docs")
        return [d for d in docs if isinstance(d, dict)] if isinstance(docs, list) else []

    @staticmethod
    def _safe_file_path(file_url: str) -> str:
        """Accept only a file path on THIS Frappe server (/files/... or /private/files/...)."""
        parsed = urlparse(str(file_url).strip())
        if parsed.scheme or parsed.netloc:      # an address on another site: never fetched
            raise FrappeError(0, "external file addresses are not allowed")
        path = unquote(parsed.path)
        if (not path.startswith(("/files/", "/private/files/"))
                or ".." in path.split("/") or "\\" in path):
            raise FrappeError(0, "file path is not allowed")
        return path

    def get_file(self, file_url: str) -> tuple[bytes, str]:
        """Download a picture stored in Frappe. Returns (bytes, mime type)."""
        path = self._safe_file_path(file_url)
        try:
            response = self.session.get(
                self.base_url + quote(path), headers={"Accept": "image/*"},
                timeout=self.timeout, stream=True,
                allow_redirects=False,   # a redirect could carry our token elsewhere
            )
        except requests.RequestException:
            raise FrappeError(0, "could not reach Frappe") from None

        try:
            if response.status_code != 200:
                raise FrappeError(response.status_code, "the file could not be fetched")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise FrappeError(0, "the file is too large")
                chunks.append(chunk)
        finally:
            response.close()

        data = b"".join(chunks)
        mime = sniff_image(data)
        if mime is None:
            raise FrappeError(0, "the file is not a supported picture")
        return data, mime


@lru_cache
def _build_service_client(site_name: str) -> FrappeClient:
    site = get_sites()[site_name]
    return FrappeClient(site.frappe_url, site.api_key.get_secret_value(),
                        site.api_secret.get_secret_value())

def get_service_client() -> FrappeClient:
    """The integration account's client (API key).

    Only for questions about structure: which employee a login belongs to, who
    reports to whom, which roles someone holds. In the web server it refuses to
    run unless a purpose was declared (see app/frappe_client/service_purpose.py) or the
    caller chose the service account explicitly with use_credentials(None).
    """
    purpose = active_purpose()
    if _strict_user_login and purpose is None and not service_account_chosen():
        raise ServicePurposeRequired()
    if purpose:
        log.debug("service account used for: %s", purpose)
    return _build_service_client(current_site().name)


def get_frappe_client() -> FrappeClient:
    """The client for reading a person's data.

    Inside a request that runs as the logged-in user (see use_credentials) this is a
    client with THEIR login, so the ERP applies their roles and permissions. Anywhere
    else, such as scripts and tests, it is the service client.
    """
    holder = current_holder()
    if holder is None:
        if _strict_user_login and not service_account_chosen():
            raise UserLoginRequired()      # fail closed: never guess "use the admin key"
        return get_service_client()
    if holder.client is None:
        holder.client = FrappeClient(credentials=holder.credentials)
    return holder.client
