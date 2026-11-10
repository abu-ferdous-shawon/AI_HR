from pathlib import Path

from app.config import settings
from app.core.policy_index import PolicyIndex
from app.core.security import ToolInputError, UserContext
from app.tools.registry import tool

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_index: PolicyIndex | None = None


def get_index() -> PolicyIndex:
    global _index
    if _index is None:
        _index = PolicyIndex(PROJECT_ROOT / settings.policy_dir)
    return _index


@tool(
    name="search_hr_policy",
    description=(
        "Search the company's HR policy documents (leave policy, handbook, "
        "rules, procedures) and return the most relevant excerpts with the file "
        f"each came from. The documents are written in {settings.policy_language}: "
        "write the search words in that language, even if the person asked in "
        "another language. If the excerpts do not answer the question, search "
        "again with different words or a synonym (for example annual, earned "
        "and privilege leave may be the same thing), up to three searches."
    ),
    parameters={"type": "object", "properties": {
        "query": {"type": "string",
                  "description": "Search words, e.g. 'earned leave entitlement'."},
    }, "required": ["query"]},
)
def search_hr_policy(ctx: UserContext, query: str | None = None) -> dict:
    query = (query or "").strip()
    if not query:
        raise ToolInputError("Please say what to look for in the policies.")
    excerpts = get_index().search(query[:300])
    if not excerpts:
        return {"excerpts": [],
                "note": "No matching text was found in the policy documents."}
    return {"excerpts": excerpts}