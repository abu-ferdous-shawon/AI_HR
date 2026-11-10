from app.core.llm import get_llm
from app.core.llm.base import LLMError, Message

SYSTEM = (
    "You are an HR assistant for a company. "
    "Reply in the same language the user writes in. Keep answers to one or two sentences."
)

llm = get_llm()
print("Using model:", llm.model)

tests = [
    "In one sentence, what is a leave balance?",
    "ছুটির ব্যালেন্স বলতে কী বোঝায়? এক বাক্যে বলুন।",
]

for question in tests:
    try:
        answer = llm.generate(SYSTEM, [Message("user", question)])
        print("\nQ:", question, "\nA:", answer)
    except LLMError as err:
        print("\nFAILED:", err)