import json
from functools import lru_cache
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests

from app.config import settings
from app.core.credentials import Credentials, current_holder
from app.core.media import sniff_image

# Safety cap: no single request may pull more than this many records.
MAX_LIMIT = 200
MAX_FILE_BYTES = 5 * 1024 * 1024   # largest picture we will download


class FrappeError(Exception):
    """Raised when Frappe cannot be reached or returns an error."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Frappe error {status_code}: {message}")
        self.status_code = status_code  # 0 means we never got a reply
        self.message = message


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
        self.base_url = (base_url or settings.frappe_url).rstrip("/")
        self.timeout = timeout
        self.acts_as_user = credentials is not None

        # A Session reuses the connection and sends the same headers each time.
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if credentials is not None:
            self.session.headers.update(credentials.headers())
        else:
            key = api_key or settings.frappe_api_key.get_secret_value()
            secret = api_secret or settings.frappe_api_secret.get_secret_value()
            self.session.headers["Authorization"] = f"token {key}:{secret}"

    # ---------- internal helpers ----------

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        try:
            response = self.session.get(url, params=params, timeout=self.timeout,
                                        allow_redirects=False)
        except requests.RequestException as exc:
            raise FrappeError(0, f"could not reach Frappe: {exc}") from exc

        if 300 <= response.status_code < 400:   # usually the login page: the session ended
            raise FrappeError(401, "the HR system redirected the request "
                                   "(the login may have expired)")
        if response.status_code >= 400:
            raise FrappeError(response.status_code, self._error_text(response))

        try:
            return response.json()
        except ValueError as exc:
            raise FrappeError(response.status_code, "reply was not valid JSON") from exc

    @staticmethod
    def _error_text(response: requests.Response) -> str:
        """Pull a short, readable reason out of a Frappe error reply."""
        try:
            body = response.json()
            text = body.get("exception") or body.get("_server_messages") or ""
        except ValueError:
            text = response.text
        return str(text)[:300] or response.reason

    # ---------- public methods ----------

    def get_list(self, doctype: str, fields: list[str] | None = None,
                 filters: dict | list | None = None, order_by: str | None = None,
                 limit: int = 20, start: int = 0) -> list[dict[str, Any]]:
        """Fetch several records, e.g. get_list('Leave Type', ['name'])."""
        params: dict[str, Any] = {
            "fields": json.dumps(fields or ["name"]),
            "limit_page_length": min(limit, MAX_LIMIT),
            "limit_start": max(start, 0),
        }
        if filters:
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by

        body = self._get(f"/api/resource/{quote(doctype)}", params)
        return body.get("data", [])

    def get_all(self, doctype: str, fields: list[str] | None = None,
                filters: dict | list | None = None, order_by: str | None = None,
                max_rows: int = 2000) -> list[dict[str, Any]]:
        """Like get_list, but reads page after page up to max_rows."""
        rows: list[dict[str, Any]] = []
        while len(rows) < max_rows:
            page = self.get_list(doctype, fields, filters, order_by,
                                 limit=MAX_LIMIT, start=len(rows))
            rows.extend(page)
            if len(page) < MAX_LIMIT:
                break
        return rows[:max_rows]

    def get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        """Fetch one full record by its ID, e.g. get_doc('Employee', 'HR-EMP-00001')."""
        path = f"/api/resource/{quote(doctype)}/{quote(name, safe='')}"
        return self._get(path).get("data", {})

    def call_method(self, method_path: str, params: dict | None = None) -> Any:
        """Call a whitelisted Frappe function, e.g. frappe.auth.get_logged_user."""
        return self._get(f"/api/method/{method_path}", params).get("message")

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
        except requests.RequestException as exc:
            raise FrappeError(0, f"could not reach Frappe: {exc}") from exc

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
def get_service_client() -> FrappeClient:
    """The integration account's client (API key).

    Used for lookups that are about structure, not a person's data: which employee
    a login belongs to, who reports to whom, which roles someone holds.
    """
    return FrappeClient()


def get_frappe_client() -> FrappeClient:
    """The client for reading a person's data.

    Inside a request that runs as the logged-in user (see use_credentials) this is a
    client with THEIR login, so the ERP applies their roles and permissions. Anywhere
    else, such as scripts and tests, it is the service client.
    """
    holder = current_holder()
    if holder is None:
        return get_service_client()
    if holder.client is None:
        holder.client = FrappeClient(credentials=holder.credentials)
    return holder.client