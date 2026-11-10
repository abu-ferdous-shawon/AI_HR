import pytest

from app.core.security import UserContext
from app.tools.registry import run_tool

CTX = UserContext("u@x.com", "HR-EMP-00001")


@pytest.mark.parametrize("args", [
    {"from_date": "2026-02-01", "to_date": "2026-01-01"},   # reversed
    {"from_date": "2025-01-01", "to_date": "2026-01-01"},   # over three months
    {"from_date": "yesterday"},                             # not a date
    {"to_date": "2026-13-45"},                              # impossible date
])
def test_bad_dates_are_refused_with_a_message(args):
    res = run_tool("get_attendance", args, CTX)
    assert "error" in res and res["error"]