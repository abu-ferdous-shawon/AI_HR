import logging
import re
from urllib.parse import urlparse

from starlette.responses import JSONResponse

log = logging.getLogger("hr_ai.hardening")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ORIGIN_RE = re.compile(r"https?://[^/\s*]+")


def parse_origins(text: str) -> list[str]:
    return [p.strip().rstrip("/").lower() for p in str(text or "").split(",") if p.strip()]


def assert_safe_config(environment: str, dev_mode: bool, origins: list[str]) -> None:
    """Refuse to start with an unsafe combination. Raises RuntimeError."""
    if str(environment).strip().lower() == "production" and dev_mode:
        # Behind nginx every request comes from 127.0.0.1, so dev mode would be open to all.
        raise RuntimeError("DEV_MODE must be false when ENVIRONMENT=production.")
    bad = [o for o in origins if not _ORIGIN_RE.fullmatch(o)]
    if bad:
        raise RuntimeError("ALLOWED_ORIGINS must be exact origins such as "
                           f"https://hrm.example.com (no wildcards or paths): {bad}")


def docs_settings(environment: str) -> dict:
    """No public API docs in production."""
    if str(environment).strip().lower() == "production":
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


class OriginGuard:
    """CSRF protection for cookie logins: state-changing requests must come from this site."""

    def __init__(self, app, allowed=()):
        self.app = app
        self.allowed = {str(o).lower() for o in allowed}

    def _ok(self, h: dict) -> bool:
        origin = h.get("origin")
        if origin is not None:
            origin = origin.strip().lower()
            if origin in self.allowed:
                return True
            host = urlparse(origin).netloc            # "null" gives ""
            return bool(host) and host == h.get("host", "").strip().lower()
        site = h.get("sec-fetch-site")
        if site in ("same-origin", "none"):
            return True
        if site in ("cross-site", "same-site"):
            return False
        # No browser hints: a bare cookie login is refused; token clients are not browsers.
        return not ("cookie" in h and "authorization" not in h)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope["headers"]}
            if not self._ok(headers):
                log.warning("blocked cross-site request", extra={"path": scope.get("path")})
                response = JSONResponse(
                    {"detail": "Request blocked: it did not come from this site."},
                    status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)