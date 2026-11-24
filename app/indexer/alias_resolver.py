"""Build deterministic DocType lookups from live schema and selected pack vocabulary."""
import re
from collections import defaultdict

from app.knowledge_packs.loader import KnowledgePack, VocabularyEntry

IRREGULAR = {"person": "people", "entry": "entries"}


def normalize(phrase: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", phrase.casefold()))


def _plural(phrase: str) -> str:
    words = phrase.split()
    if not words:
        return ""
    last = words[-1]
    if last in IRREGULAR:
        words[-1] = IRREGULAR[last]
    elif last.endswith(("ss", "sh", "ch", "x", "z")):
        words[-1] += "es"
    elif last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou":
        words[-1] = last[:-1] + "ies"
    elif not last.endswith("s"):
        words[-1] += "s"
    return " ".join(words)


def build_maps(schemas: dict[str, dict], packs: tuple[KnowledgePack, ...]) -> tuple[dict, dict]:
    aliases = defaultdict(set)
    keywords = defaultdict(set)

    def add(mapping, phrase: str, target: str):
        key = normalize(phrase)
        if key and target in schemas:
            mapping[key].add(target)

    for name, schema in schemas.items():
        for label in (name, schema["label"]):
            key = normalize(label)
            add(aliases, key, name)
            add(aliases, _plural(key), name)
            add(keywords, key, name)
        add(keywords, schema["module"], name)
        indexed_fields = {field["fieldname"]: field["label"] for field in schema["fields"]}
        for field in schema["search_fields"]:
            add(keywords, field, name)
            add(keywords, indexed_fields.get(field, ""), name)

    canonical = sorted(schemas, key=lambda name: (-len(normalize(name)), name))
    for pack in packs:
        for alias, target in pack.aliases.items():
            for name in target if isinstance(target, tuple) else (target,):
                add(aliases, alias, name)
                add(keywords, alias, name)
        for phrase, value in {**pack.kpis, **pack.vocabulary}.items():
            if isinstance(value, VocabularyEntry):
                target = value.target_doctype
            else:
                source = f" {normalize(value)} "
                target = next((name for name in canonical if f" {normalize(name)} " in source), None)
            if target:
                add(keywords, phrase, target)
    return ({key: sorted(values) for key, values in sorted(aliases.items())},
            {key: sorted(values) for key, values in sorted(keywords.items())})
