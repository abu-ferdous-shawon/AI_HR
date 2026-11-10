from app.core.calc import calculate
from app.core.security import UserContext
from app.tools.registry import tool


@tool(
    name="calculate",
    description=(
        "Do exact arithmetic. Use it for ANY calculation: leave days remaining, "
        "totals, differences, averages, percentages. Never calculate in your head. "
        "Write plain numbers, or any placeholders you were given for confidential "
        "values. Allowed: + - * / ( ) and round, min, max, abs, sum. No commas and "
        "no % signs: write a percentage as 200 * 10 / 100."
    ),
    parameters={"type": "object", "properties": {
        "expression": {"type": "string",
                       "description": "For example: 10 - 5, or ([[MASK_1]] - [[MASK_2]]) / 12."},
    }, "required": ["expression"]},
)
def calculate_tool(ctx: UserContext, expression: str | None = None) -> dict:
    return {"value": calculate(expression)}