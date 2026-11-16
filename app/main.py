import base64
import binascii
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.config import settings
from app.core.agent import run_agent
from app.core.auth import identify_user
from app.core.frappe_client import FrappeError, get_frappe_client
from app.core.llm.base import ImageData, LLMBusy, LLMError, Message
from app.core.masking import Vault
from app.core import audit, gateway
from app.core.hardening import OriginGuard, assert_safe_config, docs_settings, parse_origins
from app.core.reports import get_report
from app.core.credentials import Credentials, extract_credentials, use_credentials
from app.core.security import AccessDenied, UserContext, build_context, resolve_context
from app.core.media import get_picture
from app.registry import get_profile
from app.registry.review import masking_review
from app.tools.registry import load_all_tools

for noisy in ("httpx", "httpcore", "urllib3", "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("hr_ai.api")

@asynccontextmanager
async def lifespan(_app):
    audit.setup_app_logging()
    audit.configure()
    load_all_tools()                      # a bad profile or tool stops startup here
    for problem in masking_review(get_profile()):
        log.warning("profile review: %s", problem)
    yield
    audit.close()


_ORIGINS = parse_origins(settings.allowed_origins)
assert_safe_config(settings.environment, settings.dev_mode, _ORIGINS)   # refuse unsafe start
app = FastAPI(title="HR AI Backend", lifespan=lifespan, **docs_settings(settings.environment))
gateway.install(app)
app.add_middleware(OriginGuard, allowed=_ORIGINS)
STATIC_DIR = Path(__file__).resolve().parent / "static"

if settings.dev_mode:
    log.warning("DEV_MODE is ON: browsers on this machine can chat as any "
                "active employee. Never use this on a shared server.")
log.info("ERP access mode: %s", settings.frappe_auth_mode)
if settings.dev_mode and settings.frappe_auth_mode == "user_session":
    log.warning("Dev-mode requests have no login of their own, so they read data "
                "with the service account.")

# ---------- who is asking ----------

api_key_header = APIKeyHeader(
    name="Authorization", auto_error=False,
    description="The signed-in person's Frappe API token: token <api_key>:<api_secret>",
)


def _dev_active(request: Request) -> bool:
    """Dev mode counts only if it is switched on AND the caller is this machine."""
    if not settings.dev_mode:
        return False
    host = request.client.host if request.client else "unknown"
    if gateway.is_proxied(request):      # behind a web server every caller looks local
        log.warning("DEV_MODE is on, but the request came through a proxy, so it was refused.")
        return False
    if host in ("127.0.0.1", "::1"):
        return True
    log.warning("DEV_MODE is on, but a request came from %s (not localhost), "
                "so dev mode was refused.", host)
    return False

def _auth_audit(request: Request, ip: str, status: int, reason: str) -> None:
    audit.record("auth_failed", request_id=getattr(request.state, "request_id", "-"),
                 ip=ip, status=status, reason=reason)   # never the credentials


def _audit_download(request: Request, ctx: UserContext, kind: str, found: bool) -> None:
    audit.record("download", request_id=getattr(request.state, "request_id", "-"),
                 kind=kind, user=ctx.user_id, employee=ctx.employee_id,
                 status="ok" if found else "not_found")


def current_user(request: Request,
                 authorization: str | None = Depends(api_key_header),
                 cookie: str | None = Header(default=None),
                 x_dev_employee: str | None = Header(default=None)) -> UserContext:
    # Testing shortcut (see above). Skipped unless dev mode is active.
    if x_dev_employee and _dev_active(request):
        try:
            rows = get_frappe_client().get_list(
                "Employee", ["name"],
                filters={"name": x_dev_employee, "status": "Active"}, limit=1)
        except FrappeError as exc:
            log.warning("Dev employee lookup failed: %s", exc)
            raise HTTPException(status_code=503, detail="The HR system is not reachable right now.")
        if not rows:
            raise HTTPException(status_code=403, detail="Unknown or inactive employee.")
        return build_context(f"dev:{rows[0]['name']}", rows[0]["name"])

    # The real path: ask Frappe who these credentials belong to.
    # The real path: ask Frappe who these credentials belong to.
    ip = gateway.client_ip(request) if request is not None else "unknown"
    wait = gateway.auth_failures.blocked(ip)
    if wait:
        _auth_audit(request, ip, 429, "locked_out")
        raise HTTPException(status_code=429, detail=gateway.wait_message(wait),
                            headers={"Retry-After": str(wait)})
    try:
        user_id = identify_user(authorization, cookie)
    except AccessDenied as exc:
        gateway.auth_failures.hit(ip)
        _auth_audit(request, ip, 401, "bad_login")
        raise HTTPException(status_code=401, detail=str(exc))
    except FrappeError as exc:
        log.warning("Login check failed: %s", exc)
        _auth_audit(request, ip, 503, "erp_unreachable")
        raise HTTPException(status_code=503, detail="The HR system is not reachable right now.")
    creds = extract_credentials(authorization, cookie)
    if creds is None and settings.frappe_auth_mode == "user_session":
        gateway.auth_failures.hit(ip)          # a valid login we cannot forward: refuse, never downgrade
        _auth_audit(request, ip, 401, "no_usable_credentials")
        raise HTTPException(status_code=401, detail="Your sign-in is not valid.")
    try:
        return resolve_context(user_id, creds)
    except AccessDenied as exc:
        gateway.auth_failures.hit(ip)
        _auth_audit(request, ip, 403, "no_employee")
        raise HTTPException(status_code=403, detail=str(exc))
    except FrappeError as exc:
        log.warning("Employee lookup failed: %s", exc)
        _auth_audit(request, ip, 503, "erp_unreachable")
        raise HTTPException(status_code=503, detail="The HR system is not reachable right now.")


def request_credentials(authorization: str | None = Depends(api_key_header),
                        cookie: str | None = Header(default=None)) -> Credentials | None:
    """The caller's own ERP login, from the same headers the login check used."""
    return extract_credentials(authorization, cookie)


def _is_dev(ctx: UserContext) -> bool:
    """Contexts built by the dev shortcut (localhost only) carry this prefix."""
    return ctx.user_id.startswith("dev:")


# ---------- conversation memory (in the server's RAM) ----------

MAX_CONVERSATIONS = 500   # oldest are dropped beyond this
KEEP_USER_TURNS = 6       # how many recent questions the model remembers
_store: OrderedDict[tuple[str, str], tuple[list[Message], Vault]] = OrderedDict()
_lock = Lock()


def trim(messages: list[Message]) -> list[Message]:
    """Keep the last few questions, cutting only between whole turns."""
    starts = [i for i, m in enumerate(messages) if m.role == "user"]
    return messages[starts[-KEEP_USER_TURNS]:] if len(starts) > KEEP_USER_TURNS else messages


# ---------- the web page ----------

PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; "
                               "style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
}


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC_DIR / "index.html", headers=PAGE_HEADERS)


@app.get("/ai_chat.js", include_in_schema=False)
def widget_script():
    """The chat widget. Public on purpose: it holds no data and no secrets."""
    return FileResponse(STATIC_DIR / "ai_chat.js", media_type="application/javascript",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@app.get("/config", include_in_schema=False)
def page_config(request: Request):
    return {"dev": _dev_active(request)}


@app.get("/dev/employees", include_in_schema=False)
def dev_employees(request: Request):
    if not _dev_active(request):
        raise HTTPException(status_code=404)
    try:
        rows = get_frappe_client().get_list(
            "Employee", ["name", "employee_name"], filters={"status": "Active"}, limit=100)
    except FrappeError as exc:
        log.warning("Dev employee list failed: %s", exc)
        raise HTTPException(status_code=503, detail="The HR system is not reachable right now.")
    return [{"id": r["name"], "name": (r.get("employee_name") or "").strip()} for r in rows]


# ---------- images ----------

MAX_IMAGE_BYTES = 4 * 1024 * 1024
SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}


def _looks_like(mime: str, raw: bytes) -> bool:
    """The file's first bytes must match what it claims to be."""
    if mime == "image/webp":
        return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    return any(raw.startswith(sig) for sig in SIGNATURES[mime])


def decode_images(items: list["ImageIn"]) -> list[ImageData]:
    out: list[ImageData] = []
    for item in items:
        try:
            raw = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="An attached image could not be read.")
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="An attached image is too large (limit 4 MB).")
        if not _looks_like(item.mime_type, raw):
            raise HTTPException(status_code=422, detail="The attached file is not a valid image.")
        out.append(ImageData(item.mime_type, raw))
    return out


# ---------- chat ----------

class ImageIn(BaseModel):
    mime_type: Literal["image/jpeg", "image/png", "image/webp", "image/gif"]
    data: str = Field(min_length=1, max_length=6_000_000)  # base64 text


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    conversation_id: str | None = Field(default=None, max_length=64,
                                        pattern=r"^[A-Za-z0-9_-]+$")
    images: list[ImageIn] = Field(default_factory=list, max_length=3)


class FileInfo(BaseModel):
    id: str
    name: str
    rows: int | None = None


class PictureInfo(BaseModel):
    id: str
    name: str
    caption: str = ""


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    files: list[FileInfo] = Field(default_factory=list)
    pictures: list[PictureInfo] = Field(default_factory=list)


@app.get("/health")
def health():
    return {"status": "ok"}


# A plain "def" (not async): FastAPI runs it in a worker thread, so one slow
# model call does not freeze the other users' requests.
def _audit_chat(request: Request, ctx: UserContext, cid: str, body: ChatRequest,
                image_count: int, status: str, started: float, result=None) -> None:
    """One audit line per chat. The message text itself is never logged."""
    audit.record(
        "chat", request_id=getattr(request.state, "request_id", "-"),
        ip=gateway.client_ip(request), user=ctx.user_id, employee=ctx.employee_id,
        roles=list(ctx.roles), role_source=ctx.role_source, conversation=cid,
        status=status, duration_ms=round((time.monotonic() - started) * 1000),
        message_chars=len(body.message), images=image_count,
        tools=[audit.tool_entry(t) for t in (result.tool_log if result else [])],
        files=len(result.files) if result else 0,
        pictures=len(result.pictures) if result else 0)


# A plain "def" (not async): FastAPI runs it in a worker thread, so one slow
# model call does not freeze the other users' requests.
@app.post("/chat", response_model=ChatResponse)
def chat(request: Request, body: ChatRequest, ctx: UserContext = Depends(current_user),
         creds: Credentials | None = Depends(request_credentials)):
    started = time.monotonic()
    cid = body.conversation_id or uuid.uuid4().hex
    if (settings.frappe_auth_mode == "user_session" and not _is_dev(ctx)
            and creds is None):      # a real login we cannot forward: refuse, never downgrade
        _auth_audit(request, gateway.client_ip(request), 401, "no_usable_credentials")
        raise HTTPException(status_code=401, detail="Your sign-in is not valid.")
    wait = gateway.chat_limiter.hit(ctx.user_id)
    if wait:
        _audit_chat(request, ctx, cid, body, 0, "rate_limited", started)
        raise HTTPException(status_code=429, detail=gateway.wait_message(wait),
                            headers={"Retry-After": str(wait)})
    try:
        images = decode_images(body.images)
    except HTTPException as exc:
        _audit_chat(request, ctx, cid, body, len(body.images),
                    f"rejected_{exc.status_code}", started)
        raise
    key = (ctx.user_id, cid)  # tied to the user, so IDs cannot be borrowed

    with _lock:
        saved = _store.get(key)
    history, vault = (list(saved[0]), saved[1]) if saved else ([], None)
    try:
        own_login = creds if (settings.frappe_auth_mode == "user_session"
                              and not _is_dev(ctx)) else None
        with use_credentials(own_login):
            result = run_agent(body.message, ctx, history=history, vault=vault, images=images)
    except LLMBusy as exc:
        log.warning("LLM busy for %s: %s", ctx.user_id, exc)
        _audit_chat(request, ctx, cid, body, len(images), "llm_busy", started)
        raise HTTPException(status_code=503,
                            detail="The assistant is busy right now. Please try again in a minute.")
    except LLMError as exc:
        log.error("LLM failure for %s: %s", ctx.user_id, exc)
        _audit_chat(request, ctx, cid, body, len(images), "llm_error", started)
        raise HTTPException(status_code=503,
                            detail="The assistant is unavailable right now. Please try again.")
    with _lock:
        _store[key] = (trim(result.messages), result.vault)
        _store.move_to_end(key)
        while len(_store) > MAX_CONVERSATIONS:
            _store.popitem(last=False)

    _audit_chat(request, ctx, cid, body, len(images), "ok", started, result)
    return ChatResponse(answer=result.answer, conversation_id=cid,
                        files=[FileInfo(**f) for f in result.files],
                        pictures=[PictureInfo(**p) for p in result.pictures])


@app.get("/reports/{report_id}")
def download_report(request: Request,
                    report_id: str = PathParam(pattern=r"^[0-9a-f]{32}$"),
                    ctx: UserContext = Depends(current_user)):
    stored = get_report(report_id, ctx.user_id)  # only the person who asked gets it
    _audit_download(request, ctx, "report", stored is not None)  # only the person who asked gets it
    if stored is None:
        raise HTTPException(status_code=404, detail="This report is no longer available.")
    return FileResponse(stored.path, filename=stored.name)

@app.get("/pictures/{picture_id}")
def show_picture(request: Request,
                 picture_id: str = PathParam(pattern=r"^[0-9a-f]{32}$"),
                 ctx: UserContext = Depends(current_user)):
    picture = get_picture(picture_id, ctx.user_id)  # only the person who asked gets it
    _audit_download(request, ctx, "picture", picture is not None)  # only the person who asked gets it
    if picture is None:
        raise HTTPException(status_code=404, detail="This picture is no longer available.")
    return Response(content=picture.data, media_type=picture.mime,
                    headers={"Cache-Control": "private, no-store",
                             "X-Content-Type-Options": "nosniff"})