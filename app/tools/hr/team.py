from app.core.security import UserContext, get_team
from app.tools.registry import tool


@tool(
    name="list_my_team",
    description=(
        "List the employees who report directly to the logged-in person "
        "(ID and name). Use this to find a team member's ID before asking about "
        "their attendance. Returns an empty list if the person has no direct reports."
    ),
)
def list_my_team(ctx: UserContext) -> list[dict]:
    return get_team(ctx)