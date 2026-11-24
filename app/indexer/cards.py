"""Documents for the four FAISS indexes."""
from app.indexer.util import json_text

TIERS = ("table_index", "schema_index", "field_index", "report_index")


def _card(key: str, data: dict, text: str) -> dict:
    # Full metadata stays in the card. Limit input to the embedding provider.
    return {"key": key, "data": data, "text": text[:3000]}


def _field_text(field: dict) -> str:
    return " ".join([field["fieldname"], field["label"], field["fieldtype"], field["options"]])


def build_cards(schemas: dict[str, dict], reports: dict[str, dict], aliases: dict, keywords: dict) -> dict:
    tiers = {tier: [] for tier in TIERS}
    terms = {name: set() for name in schemas}
    for lookup in (aliases, keywords):
        for phrase, names in lookup.items():
            for name in names:
                terms[name].add(phrase)
    for name, schema in sorted(schemas.items()):
        table = {key: schema[key] for key in (
            "name", "label", "module", "description", "relationships", "istable", "issingle",
        )}
        table_text = " ".join([
            name, schema["label"], schema["module"], *sorted(terms[name]),
            schema["description"], *(rel["target"] for rel in schema["relationships"]),
        ])
        tiers["table_index"].append(_card(name, table, table_text))

        children = {
            rel["target"]: schemas[rel["target"]]
            for rel in schema["relationships"]
            if rel["kind"] in {"Table", "Table MultiSelect"} and rel["target"] in schemas
        }
        schema_text = " ".join([
            table_text, *schema["status_options"], json_text(schema["workflow_states"]),
            *(_field_text(field) for field in schema["fields"]),
            *(_field_text(field) for child in children.values() for field in child["fields"]),
        ])
        tiers["schema_index"].append(_card(name, {**schema, "child_tables": children}, schema_text))
        for field in schema["fields"]:
            key = f"{name}.{field['fieldname']}"
            tiers["field_index"].append(_card(
                key, {"doctype": name, **field}, f"{key} {name} {_field_text(field)}",
            ))
    for name, report in sorted(reports.items()):
        if not report["module"]:
            report = {**report, "module": schemas.get(report["ref_doctype"], {}).get("module", "")}
        text = " ".join([
            name, report["module"], report["report_type"], report["ref_doctype"],
            report["description"], *(_field_text(field) for field in report["filters"]),
        ])
        tiers["report_index"].append(_card(name, report, text))
    return tiers
