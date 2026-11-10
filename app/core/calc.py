import ast
import math
import operator

from app.core.masking import BANGLA_DIGITS
from app.core.security import ToolInputError

MAX_LENGTH = 300
HELP = "Use numbers with + - * / ( ) and round, min, max, abs, sum, without commas or % signs."


def _power(a, b):
    if abs(b) > 20 or abs(a) > 1e12:
        raise ToolInputError("That power is too large to calculate.")
    return a ** b


BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Pow: _power}
FUNCTIONS = {"round": round, "min": min, "max": max, "abs": abs,
             "sum": lambda *numbers: sum(numbers)}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):  # not True/False
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
        return BINARY[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in FUNCTIONS and not node.keywords):
        return FUNCTIONS[node.func.id](*[_eval(arg) for arg in node.args])
    raise ToolInputError("That is not a valid calculation. " + HELP)


def calculate(expression) -> int | float:
    text = str(expression or "").translate(BANGLA_DIGITS).strip()   # Bangla digits work too
    if not text:
        raise ToolInputError("Please give a calculation.")
    if len(text) > MAX_LENGTH:
        raise ToolInputError("That calculation is too long.")
    if "%" in text:
        raise ToolInputError("Write percentages as numbers, for example 200 * 10 / 100.")
    try:
        value = _eval(ast.parse(text, mode="eval"))
    except SyntaxError:
        raise ToolInputError("That is not a valid calculation. " + HELP) from None
    except ZeroDivisionError:
        raise ToolInputError("Cannot divide by zero.") from None
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ToolInputError("That calculation could not be done. " + HELP) from None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolInputError("That calculation could not be done. " + HELP)
        return round(value, 4)
    return value