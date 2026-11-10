import pytest

from app.core.calc import calculate
from app.core.security import ToolInputError


@pytest.mark.parametrize("expression, expected", [
    ("10 - 5", 5), ("(10 - 5) * 2", 10), ("200 * 10 / 100", 20.0), ("2 ** 3", 8),
    ("round(10 / 3, 2)", 3.33), ("max(3, 9, 4)", 9), ("sum(1, 2, 3.5)", 6.5),
    ("-5 + 8", 3), ("৫০০ - ১২০", 380), ("(50000) - (5000)", 45000),
])
def test_valid_calculations(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize("expression", [
    "", "__import__('os').system('ls')", "().__class__", "open('x')", "x + 1",
    "10%", "50,000 + 1", "1 < 2", "2 ** 100", "10 ** 99999999", "1 / 0",
    "5 +", "a" * 400, "[1, 2][0]", "lambda: 1", "10 if 1 else 2", "True + 1",
])
def test_everything_else_is_refused(expression):
    with pytest.raises(ToolInputError):
        calculate(expression)