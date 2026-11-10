from fastapi.testclient import TestClient

from app.core.security import UserContext
from app.main import app, current_user

# For THIS test only: pretend the login check already happened and said
# "this is HR-EMP-00001". The real server never does this.
app.dependency_overrides[current_user] = lambda: UserContext(
    user_id="test-only", employee_id="HR-EMP-00001"
)
client = TestClient(app)


def check(label, ok, detail=""):
    print(f"{label}: {'PASS' if ok else 'FAIL'} {detail}")


r1 = client.post("/chat", json={"message": "How many casual leaves do I have left?"})
check("1 chat gives an answer", r1.status_code == 200 and "15" in r1.json().get("answer", ""), r1.json())

if r1.status_code == 200:
    cid = r1.json()["conversation_id"]
    r2 = client.post("/chat", json={"message": "And sick leave?", "conversation_id": cid})
    check("2 follow-up understood", r2.status_code == 200 and "10" in r2.json().get("answer", ""), r2.json())
    check("3 same conversation id kept", r2.json().get("conversation_id") == cid)

r = client.post("/chat", json={"message": ""})
check("4 empty message refused", r.status_code == 422)

r = client.post("/chat", json={"message": "hi", "conversation_id": "../../etc"})
check("5 odd conversation id refused", r.status_code == 422)