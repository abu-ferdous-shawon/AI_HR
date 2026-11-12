import sys

from app.core.security import UserContext
from app.records import fetch_records

employee, record_type = sys.argv[1], sys.argv[2]
scope = sys.argv[3] if len(sys.argv) > 3 else None
try:
    res = fetch_records(UserContext("manual", employee), record_type, scope=scope)
except Exception as exc:
    raise SystemExit(f"{type(exc).__name__}: {exc}")
print({k: res[k] for k in ("outcome", "record_type", "scope", "count", "truncated",
                           "filters_used") if k in res})
if res.get("records"):
    print("fields:", sorted(res["records"][0]))