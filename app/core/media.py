import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

TTL_SECONDS = 1800   # pictures are forgotten after 30 minutes
MAX_ITEMS = 50       # and only the newest 50 are kept

_SIGNATURES = (
    ("image/jpeg", (b"\xff\xd8\xff",)),
    ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ("image/gif", (b"GIF87a", b"GIF89a")),
)


def sniff_image(data: bytes) -> str | None:
    """Decide the picture type from the file's first bytes. SVG is deliberately refused."""
    for mime, signatures in _SIGNATURES:
        if data.startswith(signatures):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class Picture:
    owner: str      # the user who asked for it; only they can fetch it
    data: bytes
    mime: str
    name: str
    created: float


_pictures: OrderedDict[str, Picture] = OrderedDict()
_lock = Lock()


def _expire() -> None:
    cutoff = time.time() - TTL_SECONDS
    with _lock:
        for key in [k for k, p in _pictures.items() if p.created < cutoff]:
            del _pictures[key]


def save_picture(owner: str, name: str, data: bytes, mime: str) -> dict:
    _expire()
    picture_id = uuid.uuid4().hex   # unguessable, and the owner is checked too
    with _lock:
        _pictures[picture_id] = Picture(owner, data, mime, name, time.time())
        while len(_pictures) > MAX_ITEMS:
            _pictures.popitem(last=False)
    return {"id": picture_id, "name": name}


def get_picture(picture_id: str, owner: str) -> Picture | None:
    """Return the picture only if it belongs to this user and has not expired."""
    with _lock:
        picture = _pictures.get(picture_id)
    if picture is None or picture.owner != owner or picture.created < time.time() - TTL_SECONDS:
        return None
    return picture