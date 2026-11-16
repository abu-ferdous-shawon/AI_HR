from app.core.masking import is_confidential
from app.registry.models import Profile

# Names that usually hold free text, which can contain personal details.
REVIEW_WORDS = {"explanation", "feedback", "comment", "comments", "remark", "remarks",
                "note", "notes", "description", "summary", "detail", "details",
                "resolution", "complaint", "diagnosis", "medical"}


def _looks_like_free_text(name: str) -> bool:
    return any(word in REVIEW_WORDS for word in name.lower().split("_"))


def masking_review(profile: Profile) -> list[str]:
    """Things in a profile that could let a confidential value through. Empty = nothing to review."""
    own = {n.lower() for n in profile.confidential_field_names()}

    def masked(name: str) -> bool:
        return name.lower() in own or is_confidential(name)

    problems: list[str] = []
    for key, rt in profile.enabled_types().items():
        for field in rt.fields:
            if _looks_like_free_text(field) and not masked(field):
                problems.append(f"{key}.{field} looks like free text but is not in "
                                "confidential_fields")
        for table, child in rt.child_tables.items():
            for field in child.fields:
                if _looks_like_free_text(field) and not masked(field):
                    problems.append(f"{key}.{table}.{field} looks like free text but is not in "
                                    "the table's confidential_fields")
        for field in rt.group_by_allowed:
            if masked(field):
                problems.append(f"{key}: grouping by the confidential field '{field}' shows "
                                "placeholders instead of group names")
        for name, agg in rt.aggregations.items():
            if agg.field and masked(agg.field) and not agg.money:
                problems.append(f"{key}.{name} totals the confidential field '{agg.field}' "
                                "without money: true (the result is masked anyway)")
    return problems