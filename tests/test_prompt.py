import re

import pytest

from app.core.agent import build_system_prompt
from app.core.frappe_client import FrappeError
from app.core.masking import Vault
from app.core.security import AccessDenied, ToolInputError
from app.tools import registry
from tests.records_helpers import ME, World, leave_row

PROMPT = build_system_prompt(Vault())


def rules() -> dict[int, str]:
    parts = re.split(r"^(\d+)\. ", PROMPT.split("Rules:", 1)[1], flags=re.M)
    return {int(parts[i]): " ".join(parts[i + 1].split()) for i in range(1, len(parts) - 1, 2)}


def named_tools(text: str) -> set[str]:
    names = set(re.findall(r"\b(?:get|list|search|summarize|create|show)_[a-z_]+\b", text))
    if re.search(r"\bcalculate\b", text):
        names.add("calculate")
    if "company_summary" in text:
        names.add("company_summary")
    return names


def registered() -> set[str]:
    return {t["name"] for t in registry.get_tool_specs()}


def all_keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from all_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from all_keys(item)


# ---------- shape ----------

def test_the_rules_are_numbered_without_gaps():
    assert list(rules()) == list(range(1, 23))


def test_rule_7_and_rule_17_keep_their_agreed_content():
    found = rules()
    assert "numbers, dates, document IDs" in found[7]
    assert "exactly as they appear" in found[7]
    assert "Translate display labels and readable values" in found[7]
    assert "never do arithmetic in your head" in found[17].lower()
    assert "calculate" in found[17] and "pending_approval" in found[17]


def test_the_base_prompt_never_mentions_placeholders():
    assert "placeholder" not in PROMPT.lower() and "MASK_" not in PROMPT


def test_today_is_filled_in():
    assert "{today}" not in PROMPT and re.search(r"\d{4}-\d{2}-\d{2}", PROMPT)


# ---------- tools ----------

def test_every_tool_the_prompt_names_exists():
    missing = named_tools(PROMPT) - registered()
    assert not missing, f"The prompt names tools that do not exist: {sorted(missing)}"


def test_every_registered_tool_is_named_in_the_prompt():
    unnamed = registered() - named_tools(PROMPT)
    assert not unnamed, f"Add {sorted(unnamed)} to the system prompt (rule 19 lists the tools)"


# ---------- answers the prompt tells the model to read ----------

@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def test_the_fields_the_prompt_mentions_exist_in_real_answers(world):
    world.rows["Pay Thing"] = [{"name": "S1", "employee": "EMP-1", "start_date": "2026-03-01",
                                "net_pay": 1.0, "dept": "IT"}]
    world.docs["S1"] = {"earnings": [{"salary_component": "Basic", "amount": 1.0}]}
    listing = world.fetch("slip")
    empty = world.fetch("event", from_date="2026-01-01")        # a date the type does not use
    world.rows["Leave Thing"] = [leave_row(f"L{i}", employee_name=f"Person {i}")
                                 for i in range(51)]
    total = world.summarize(aggregation="total_days", group_by="employee_name")

    assert listing["outcome"] == "records" and empty["outcome"] == "no_records"
    seen = set(all_keys([listing, empty, total]))
    for key in ("outcome", "truncated", "details_included", "filters_used", "ignored",
                "records_counted", "numeric_records", "statuses_included", "groups_truncated"):
        assert key in seen, f"the engine no longer returns '{key}'"
        assert key in rules()[20] or key in rules()[21], f"the prompt no longer mentions '{key}'"
    assert "no_records" in rules()[20] and "records" in rules()[20]


def test_the_refusals_the_prompt_describes_are_the_ones_the_system_gives(world):
    def refused(call):
        with pytest.raises(AccessDenied) as err:
            call()
        return str(err.value)

    messages = {
        "level": refused(lambda: world.fetch("slip", scope="team")),
        "hr": refused(lambda: world.fetch(scope="company")),
        "no_reports": refused(lambda: world.fetch(scope="team")),
    }
    world.team = [{"name": "EMP-2", "employee_name": "Rahim"}]
    messages["not_report"] = refused(lambda: world.fetch(scope="team", employee="Karim"))
    world.error = FrappeError(403, "x")
    messages["erp"] = refused(lambda: world.fetch())

    wording = {   # key: (what the system says, what the prompt tells the model to expect)
        "level": ("not available at the", "not available at that level"),
        "hr": ("HR only", "HR only"),
        "no_reports": ("no direct reports", "no direct reports"),
        "not_report": ("not one of your direct reports", "not one of your direct reports"),
        "erp": ("did not allow", "did not allow"),
    }
    for key, (said, expected) in wording.items():
        assert said in messages[key], f"the system no longer says '{said}' ({key})"
        assert expected in rules()[20], f"rule 20 no longer mentions '{expected}'"


def test_the_other_messages_match_rule_20(world, monkeypatch):
    text = rules()[20]
    for call in (lambda: world.fetch("weather"), lambda: world.fetch(status="maybe"),
                 lambda: world.summarize(aggregation="nope"),
                 lambda: world.summarize(group_by="employee")):
        with pytest.raises(ToolInputError) as err:
            call()
        assert re.search(r"Available:|must be one of", str(err.value))
    assert "valid record types, statuses, totals or groupings" in text

    world.rows["Leave Thing"] = [leave_row(f"L{i}") for i in range(101)]
    with pytest.raises(ToolInputError) as err:
        world.summarize()
    assert "too many" in str(err.value) and "to total" in str(err.value)
    assert "too many records to total" in text

    world.hr = True
    world.employees = [{"name": "A", "employee_name": "X"}, {"name": "B", "employee_name": "Y"}]
    with pytest.raises(ToolInputError) as err:
        world.fetch(scope="company", employee="x")
    assert "More than one employee" in str(err.value)
    assert "matching a name" in text

    def boom(ctx):
        raise FrappeError(500, "boom")

    monkeypatch.setitem(registry._TOOLS, "boom", registry.Tool(
        "boom", "d", {"type": "object", "properties": {}}, boom))
    failed = registry.run_tool("boom", {}, ME)["error"]
    assert "could not complete" in failed and "could not complete" in text
