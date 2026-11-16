import time

from app.core.agent import build_system_prompt
from app.core.language import language_hint
from app.core.llm import get_llm
from app.core.llm.base import LLMError, Message
from app.core.masking import Vault
from app.tools.registry import get_tool_specs, load_all_tools

PAUSE = 4.5          # seconds between questions: the free tier allows 15 requests a minute
OWN = [None, "self"]  # no scope given, or the person's own


def record_case(question, kind, **args):
    return (question, {"tools": ["get_employee_records"],
                       "args": {"record_type": kind, "scope": OWN, **args}})


CASES = [
    ("How many casual leaves do I have left?", {"tools": ["get_my_leave_balance"]}),
    ("Show my leave applications for March 2026", {"tools": ["get_my_leave_history"]}),
    record_case("Show my promotions", ["employee_promotion"]),
    record_case("amar podonnoti hoyeche kokhono?", ["employee_promotion"]),
    record_case("Show my training history", ["training_event", "training_result"]),
    record_case("What is the status of my grievance?", ["employee_grievance"]),
    ("How many leave applications were approved this year?",
     {"tools": ["summarize_employee_records"], "args": {"record_type": ["leave_application"]}}),
    ("Show the attendance of my team for last week",
     {"tools": ["get_employee_records"],
      "args": {"record_type": ["attendance"], "scope": ["team"]}}),
    ("Show Rahim Uddin's appraisal",
     {"tools": ["get_employee_records"],
      "args": {"record_type": ["appraisal"], "employee": "~rahim",
               "scope": ["team", "company"]}}),
    ("How many employees took sick leave in October 2026?",
     {"tools": ["summarize_employee_records", "company_summary"]}),
    ("What is the total payroll cost this month?", {"tools": ["company_summary"]}),
    ("When is the next holiday?", {"tools": ["get_holidays"]}),
    ("Can I carry forward unused annual leave?", {"tools": ["search_hr_policy"]}),
]


def _norm(value):
    return None if value is None else str(value).strip().lower()


def _arg_ok(actual, wanted) -> bool:
    if isinstance(wanted, str) and wanted.startswith("~"):      # "~rahim": contains, any case
        return actual is not None and wanted[1:].lower() in str(actual).lower()
    options = wanted if isinstance(wanted, list) else [wanted]
    return _norm(actual) in {_norm(o) for o in options}          # None means "not given"


def judge(expect: dict, calls: list) -> tuple[bool, str]:
    """Did any tool call match the expectation? Returns (ok, what was chosen instead)."""
    if not calls:
        return False, "no tool was called"
    for call in calls:
        if call.name in expect["tools"] and all(
                _arg_ok(call.args.get(key), wanted)
                for key, wanted in expect.get("args", {}).items()):
            return True, ""
    first = calls[0]
    return False, f"called {first.name} {dict(first.args)}"


def describe(expect: dict) -> str:
    parts = [" or ".join(expect["tools"])]
    for key, wanted in expect.get("args", {}).items():
        parts.append(f"{key}={wanted}")
    return "; ".join(parts)


def main() -> None:
    load_all_tools()
    llm, tools = get_llm(), get_tool_specs()
    passed = 0
    for number, (question, expect) in enumerate(CASES, start=1):
        system = build_system_prompt(Vault(), language_hint(question))
        try:
            reply = llm.generate(system, [Message("user", question)], tools)
        except LLMError as exc:
            print(f"{number:>2} ERROR {question}\n      {exc}")
            continue
        ok, _ = judge(expect, reply.tool_calls)
        passed += ok
        chosen = ", ".join(f"{c.name}{dict(c.args)}" for c in reply.tool_calls) or "(no tool)"
        print(f"{number:>2} {'PASS ' if ok else 'MISS '} {question}\n      chose: {chosen}")
        if not ok:
            print(f"      wanted: {describe(expect)}")
        time.sleep(PAUSE)
    print(f"\n{passed} of {len(CASES)} questions were routed as expected.")


if __name__ == "__main__":
    main()