"""Build deterministic DocType lookups from live schema and selected pack vocabulary."""
import re
from collections import defaultdict
from pathlib import Path

import yaml

from app.core.sites import PACK_RE

BUILTIN_PACKS = Path(__file__).resolve().parents[1] / "knowledge_packs"
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


def load_packs(names: list[str], directory: str) -> list[dict]:
    """Read the vocabulary and alias sections used by the metadata indexer."""
    packs = []
    for name in dict.fromkeys(names):
        if not PACK_RE.fullmatch(name):
            raise ValueError(f"Invalid knowledge pack name {name!r}")
        path = Path(directory).expanduser() / f"{name}.yaml" if directory else None
        if path is None or not path.exists():
            path = BUILTIN_PACKS / f"{name}.yaml"
        try:
            pack = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise ValueError(f"Unable to read knowledge pack {path}") from None
        if not isinstance(pack, dict) or pack.get("pack_name") != name:
            raise ValueError(f"Expected pack_name: {name} in {path}")
        for section in ("aliases", "vocabulary", "never_send"):
            if not isinstance(pack.get(section, {}), dict):
                raise ValueError(f"{path}: {section} must be a mapping")
        aliases, vocabulary, never_send = (pack.get(section, {}) for section in
                                          ("aliases", "vocabulary", "never_send"))
        for phrase, targets in aliases.items():
            if not isinstance(phrase, str) or not _strings(targets if isinstance(targets, list) else [targets]):
                raise ValueError(f"{path}: aliases must map strings to DocType names")
        vocabulary = {
            phrase: value.get("target_doctype") if isinstance(value, dict) else value
            for phrase, value in vocabulary.items()
        }
        if not all(isinstance(phrase, str) and isinstance(value, str)
                   for phrase, value in vocabulary.items()):
            raise ValueError(f"{path}: vocabulary requires text or a target_doctype")
        global_fields = never_send.get("global_fields", [])
        doctype_fields = never_send.get("doctype_fields", {})
        if not (_strings(global_fields) and isinstance(doctype_fields, dict)
                and all(isinstance(dt, str) and _strings(fields) for dt, fields in doctype_fields.items())):
            raise ValueError(f"{path}: never_send must list field names")
        packs.append({
            "pack_name": name, "aliases": aliases, "vocabulary": vocabulary,
            "never_send": {"global_fields": global_fields, "doctype_fields": doctype_fields},
        })
    return packs


def _strings(values) -> bool:
    return isinstance(values, list) and all(isinstance(value, str) for value in values)


def excluded_fields(packs: list[dict], doctype: str) -> set[str]:
    fields = set()
    for pack in packs:
        block = pack.get("never_send", {})
        fields.update(block.get("global_fields", []))
        fields.update(block.get("doctype_fields", {}).get(doctype, []))
    return fields


def build_maps(schemas: dict[str, dict], packs: list[dict]) -> tuple[dict, dict]:
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
        for alias, target in pack.get("aliases", {}).items():
            for name in target if isinstance(target, list) else [target]:
                add(aliases, alias, name)
                add(keywords, alias, name)
        for phrase, value in pack.get("vocabulary", {}).items():
            source = f" {normalize(value)} "
            target = next((name for name in canonical if f" {normalize(name)} " in source), None)
            if target:
                add(keywords, phrase, target)
    return ({key: sorted(values) for key, values in sorted(aliases.items())},
            {key: sorted(values) for key, values in sorted(keywords.items())})
