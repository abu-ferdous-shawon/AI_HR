import os

from app.core.credentials import Credentials, extract_credentials

HOW_TO = (
    "Set the person's own login first, in this terminal only:\n"
    "  export HR_TEST_TOKEN='token <api_key>:<api_secret>'\n"
    "or, to test the cookie path the embedded chat will use:\n"
    "  export HR_TEST_SID='<value of the sid cookie>'"
)


def credentials_from_env(environ=None) -> Credentials | None:
    """The test person's own login, from variables you set yourself. A token wins over a sid."""
    env = os.environ if environ is None else environ
    token = (env.get("HR_TEST_TOKEN") or "").strip()
    sid = (env.get("HR_TEST_SID") or "").strip()
    return extract_credentials(token or None, f"sid={sid}" if sid else None)