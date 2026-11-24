import hashlib
import json


def json_text(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()
