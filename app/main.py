import base64
import binascii
import logging
import uuid
from collections import OrderedDict
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
from app.core.reports import get_report
from app.core.security import AccessDenied, UserContext, resolve_context
from app.core.media import get_picture
from app.tools.registry import load_all_tools

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "urllib3", "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("hr_ai.api")

load_all_tools()  # fail at startup, not in the middle of a chat

app = FastAPI(title="HR AI Backend")
STATIC_DIR = Path(__file__).resolve().parent / "static"

if settings.dev_mode:
    log.warning("DEV_MODE is ON: browsers on this machine can chat as any "
                "active employee. Never use this on a shared server.")

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
    if host in ("127.0.0.1", "::1"):
        return True
    log.warning("DEV_MODE is on, but a request came from %s (not localhost), "
                "so dev mode was refused.", host)
    return False


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
        return UserContext(user_id=f"dev:{rows[0]['name']}", employee_id=rows[0]["name"])

    # The real path: ask Frappe who these credentials belong to.
    try:
        user_id = identify_user(authorization, cookie)
    except AccessDenied as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except FrappeError as exc:
        log.warning("Login check failed: %s", exc)
        raise HTTPException(status_code=503, detail="The HR system is not reachable right now.")
    try:
        return resolve_context(user_id)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except FrappeError as exc:
        log.warning("Employee lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="The HR system is not reachable right now.")


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
@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, ctx: UserContext = Depends(current_user)):
    images = decode_images(body.images)
    cid = body.conversation_id or uuid.uuid4().hex
    key = (ctx.user_id, cid)  # tied to the user, so IDs cannot be borrowed

    with _lock:
        saved = _store.get(key)
    history, vault = (list(saved[0]), saved[1]) if saved else ([], None)
    try:
        result = run_agent(body.message, ctx, history=history, vault=vault, images=images)
    except LLMBusy as exc:
        log.warning("LLM busy for %s: %s", ctx.user_id, exc)
        raise HTTPException(status_code=503,
                            detail="The assistant is busy right now. Please try again in a minute.")
    except LLMError as exc:
        log.error("LLM failure for %s: %s", ctx.user_id, exc)
        raise HTTPException(status_code=503,
                            detail="The assistant is unavailable right now. Please try again.")

    with _lock:
        _store[key] = (trim(result.messages), result.vault)
        _store.move_to_end(key)
        while len(_store) > MAX_CONVERSATIONS:
            _store.popitem(last=False)

    return ChatResponse(answer=result.answer, conversation_id=cid,
                        files=[FileInfo(**f) for f in result.files],
                        pictures=[PictureInfo(**p) for p in result.pictures])


@app.get("/reports/{report_id}")
def download_report(report_id: str = PathParam(pattern=r"^[0-9a-f]{32}$"),
                    ctx: UserContext = Depends(current_user)):
    stored = get_report(report_id, ctx.user_id)  # only the person who asked gets it
    if stored is None:
        raise HTTPException(status_code=404, detail="This report is no longer available.")
    return FileResponse(stored.path, filename=stored.name)

@app.get("/pictures/{picture_id}")
def show_picture(picture_id: str = PathParam(pattern=r"^[0-9a-f]{32}$"),
                 ctx: UserContext = Depends(current_user)):
    picture = get_picture(picture_id, ctx.user_id)  # only the person who asked gets it
    if picture is None:
        raise HTTPException(status_code=404, detail="This picture is no longer available.")
    return Response(content=picture.data, media_type=picture.mime,
                    headers={"Cache-Control": "private, no-store",
                             "X-Content-Type-Options": "nosniff"})