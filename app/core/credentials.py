import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_AUTH_RE = re.compile(r"(?:token\s+[^\s:]+:\S+|bearer\s+\S+)", re.IGNORECASE)
_SID_RE = re.compile(r"(?:^|;\s*)sid=([^;]*)")
_SID_OK = re.compile(r"[A-Za-z0-9_.~-]{8,200}")


@dataclass(frozen=True)
class Credentials:
    """The caller's own login for the ERP: an API token or a session id.

    It is never logged and never sent to the model, so repr shows nothing.
    """
    authorization: str | None = field(default=None, repr=False)
    sid: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "Credentials(<hidden>)"

    def headers(self) -> dict[str, str]:
        if self.authorization:
            return {"Authorization": self.authorization}
        if self.sid:
            return {"Cookie": f"sid={self.sid}"}     # only the session id, no other cookies
        return {}


def extract_credentials(authorization: str | None, cookie: str | None) -> Credentials | None:
    """Same precedence as the login check: an Authorization header wins over a cookie."""
    if authorization and str(authorization).strip():
        value = str(authorization).strip()
        return Credentials(authorization=value) if _AUTH_RE.fullmatch(value) else None
    if cookie:
        match = _SID_RE.search(str(cookie))
        sid = match.group(1).strip() if match else ""
        if sid and sid != "Guest" and _SID_OK.fullmatch(sid):
            return Credentials(sid=sid)
    return None


@dataclass
class _Holder:
    credentials: Credentials
    client: object | None = None     # built on first use, closed when the block ends


_CURRENT: ContextVar[_Holder | None] = ContextVar("erp_login", default=None)


def current_holder() -> _Holder | None:
    return _CURRENT.get()


@contextmanager
def use_credentials(credentials: Credentials | None):
    """Run a block as the logged-in user. None means the service account, explicitly."""
    holder = _Holder(credentials) if credentials is not None else None
    token = _CURRENT.set(holder)
    try:
        yield
    finally:
        _CURRENT.reset(token)
        if holder is not None and holder.client is not None:
            holder.client.session.close()