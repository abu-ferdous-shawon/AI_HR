import argparse
import logging

from app.core.agent import run_agent
from app.core.llm.base import LLMError, Message
from app.core.security import AccessDenied, UserContext, resolve_context
from app.tools.registry import load_all_tools

KEEP_USER_TURNS = 6  # how many recent questions the assistant remembers

logging.basicConfig(level=logging.WARNING)  # hide technical log lines


def trim(messages: list[Message]) -> list[Message]:
    """Keep the last few questions, cutting only between whole turns."""
    starts = [i for i, m in enumerate(messages) if m.role == "user"]
    return messages[starts[-KEEP_USER_TURNS]:] if len(starts) > KEEP_USER_TURNS else messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the HR assistant.")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--login", help="Frappe login email, e.g. test@mail.com")
    who.add_argument("--employee", help="Employee ID, e.g. HR-EMP-00001 (testing only)")
    args = parser.parse_args()

    load_all_tools()

    # Decide who is "asking". The assistant treats this person as the user.
    try:
        if args.employee:
            ctx = UserContext(user_id="local-test", employee_id=args.employee)
        else:
            ctx = resolve_context(args.login or "test@mail.com")
    except AccessDenied as exc:
        raise SystemExit(f"Cannot start: {exc}")

    print(f"Chatting as {ctx.employee_id}.")
    print("Type your question and press Enter. Commands: /new  /tools  /quit\n")

    history: list[Message] = []
    show_tools = False
    vault = None 

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not question:
            continue
        if question == "/quit":
            print("Bye.")
            break
        if question == "/new":
            history = []
            vault = None 
            print("(Started a new conversation.)\n")
            continue
        if question == "/tools":
            show_tools = not show_tools
            print(f"(Tool details {'on' if show_tools else 'off'}.)\n")
            continue

        try:
            result = run_agent(question, ctx, history=history)
             
        except LLMError as exc:
            print(f"The assistant is unavailable right now: {exc}\n")
            continue

        history = trim(result.messages)
        vault = result.vault 
        if show_tools:
            for t in result.tool_log:
                status = "ok" if t["ok"] else f"refused: {t['error']}"
                print(f"   [tool] {t['tool']} {t['args']} -> {status}")
        print(f"\nAssistant: {result.answer}\n")


if __name__ == "__main__":
    main()