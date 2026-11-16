import ipaddress
import uuid

from fastapi.responses import JSONResponse

from app.config import settings
from app.core.ratelimit import RateLimiter

chat_limiter = RateLimiter(settings.rate_limit_chat_per_minute)
auth_failures = RateLimiter(settings.rate_limit_auth_fail_per_minute)

LOCAL = ("127.0.0.1", "::1")
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
BODY_METHODS = ("POST", "PUT", "PATCH")
NO_STORE = ("/chat", "/reports", "/pictures", "/config", "/dev")


def is_proxied(request) -> bool:
    return any(request.headers.get(h) for h in PROXY_HEADERS)


def client_ip(request) -> str:
    """The caller's address. Proxy headers count only if TRUST_PROXY is on and the
    request really came from this machine."""
    peer = request.client.host if request.client else "unknown"
    if settings.trust_proxy and peer in LOCAL:
        real = (request.headers.get("x-real-ip") or "").strip()
        if not real:
            real = (request.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return peer


def wait_message(seconds: int) -> str:
    return f"Too many requests. Please wait {seconds} seconds and try again."


def _size_problem(request):
    if request.method not in BODY_METHODS:
        return None
    length = request.headers.get("content-length")
    if length is None:
        return 411, "Content-Length is required."
    if not length.isdigit():
        return 400, "Invalid Content-Length."
    if int(length) > settings.max_request_bytes:
        return 413, "The request is too large."
    return None


def install(app) -> None:
    @app.middleware("http")
    async def gateway_middleware(request, call_next):
        request_id = uuid.uuid4().hex          # never taken from the client
        request.state.request_id = request_id
        problem = _size_problem(request)
        if problem:
            return JSONResponse({"detail": problem[1]}, status_code=problem[0],
                                headers={"X-Request-ID": request_id})
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith(NO_STORE):
            response.headers.setdefault("Cache-Control", "no-store")
        return response