"""Why the integration (service) account is being used.

The service account can see more than any single person, so it may only be used
for questions about structure (who is this login, who reports to whom, which
roles), never to read a person's own data. Every use must name its purpose.
"""
import functools
import logging
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger("hr_ai.service")

PURPOSES = {
    "identity": "find which employee a login belongs to",
    "roles": "look up a person's roles",
    "team": "list who reports to a person",
    "employee_search": "find an employee by name (HR only, checked by the caller)",
    "indexing": "read DocType metadata for the search indexes (background job)",
}

_ACTIVE: ContextVar[str | None] = ContextVar("service_purpose", default=None)


def active_purpose() -> str | None:
    return _ACTIVE.get()


def _check(name: str) -> None:
    if name not in PURPOSES:
        raise ValueError(f"Unknown service purpose '{name}'. Allowed: {', '.join(PURPOSES)}")


@contextmanager
def service_purpose(name: str):
    """Inside this block the service account may be used, for this purpose only."""
    _check(name)
    token = _ACTIVE.set(name)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def for_purpose(name: str):
    """Decorator form: the whole function runs with this purpose declared."""
    _check(name)                      # a typo fails at import time, not in production

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with service_purpose(name):
                return func(*args, **kwargs)
        return wrapper
    return decorator