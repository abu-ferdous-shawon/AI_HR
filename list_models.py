from google import genai

from app.config import settings

client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())

print("Models your key can use for text generation:\n")
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" in actions:
        print(m.name.replace("models/", ""))