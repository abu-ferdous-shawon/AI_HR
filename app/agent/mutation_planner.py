"""Interpret one chat write request as a proposal. The model has no confirm API."""
import json
import re
import time

from pydantic import ValidationError

from app.core.agent import AgentResult
from app.core.language import language_hint
from app.core.llm import get_llm
from app.core.llm.base import LLMProvider, Message
from app.core.masking import TOKEN_RE, Vault
from app.auth.security import UserContext
from app.core.sites import current_site
from app.core.turn_trace import current_trace
from app.frappe_client.client import FrappeClient, FrappeError
from app.indexer.alias_resolver import normalize
from app.indexer.sync import open_snapshot, search_metadata
from app.indexer.vector_store import read_json
from app.knowledge_packs.loader import active_packs
from app.mutations.service import (
    MutationFailure, MutationRequest, Operation, describe_mutation_fields, frappe_failure, load_mutation_schema,
    prepare_mutation, writable_doctype,
)

_VERBS: dict[Operation, str] = {
    "create": r"create|add|insert|make|apply (?:for )?(?:leave|pto)",
    "update": r"update|edit|change|set",
    "submit": r"submit",
    "cancel": r"cancel",
    "delete": r"delete|remove",
}
_TOOL = {
    "name": "propose_mutation",
    "description": "Prepare one change for review. Supply only values the user specified.",
    "parameters": MutationRequest.model_json_schema(),
    "json_schema": True,
}
_EXPLICIT_DOCTYPE = re.compile(r'\bDocType\s+"([^"\r\n]{1,140})"', re.I)
_MY_PHONE_UPDATE = re.compile(
    r"(?:please\s+)?(?:update|change|set|edit)\s+my\s+"
    r"(?:phone|mobile|cell(?:\s+phone)?)(?:\s+number)?\s+to\s+"
    r"(?P<number>\+?\(?[0-9][0-9() \t-]*)[.!]?",
    re.I,
)


def _targets(question: str) -> list[str]:
    explicit = _EXPLICIT_DOCTYPE.findall(question)
    if explicit:
        return sorted({name.strip() for name in explicit if name.strip()})

    site = current_site()
    packs = active_packs(site)
    known = set(site.write_doctypes)
    aliases = {}
    try:
        with open_snapshot(site, packs) as (path, manifest):
            known.update(manifest["schemas"])
            aliases = read_json(path / "alias_map.json")
    except (RuntimeError, OSError, ValueError):
        pass  # Explicit DocType names can still be verified directly in Frappe.

    text = f" {normalize(question)} "
    matches = {target for target in known if f" {normalize(target)} " in text}
    if matches:
        return sorted(matches)
    for alias, targets in aliases.items():
        if f" {alias} " in text:
            matches.update(targets)
    for pack in packs:
        for alias, targets in pack.aliases.items():
            if f" {normalize(alias)} " in text:
                matches.update(targets if isinstance(targets, tuple) else (targets,))
    return sorted(matches.intersection(known))


def _literal_operation(question: str) -> Operation | None:
    patterns = {name: re.search(rf"\b(?:{pattern})\b", question, re.IGNORECASE)
                for name, pattern in _VERBS.items()}
    matches = [(match.start(), name) for name, match in patterns.items() if match]
    return min(matches)[1] if matches else None


def _field_suggestions(question: str, fields: list[dict]) -> list[str]:
    words = set(normalize(question).split())

    def relevance(field: dict) -> int:
        terms = normalize(f"{field['label'] or ''} {field['fieldname']}").split()
        return len(words.intersection(terms))

    ranked = sorted(fields, key=relevance, reverse=True)
    return [field["label"] or field["fieldname"] for field in ranked[:3]]


def _clarify_target(question: str, operation: Operation, targets: list[str], client: FrappeClient) -> str:
    if not targets:
        search_text = TOKEN_RE.sub("", Vault().mask_typed(question)).strip()
        try:
            hits = search_metadata(current_site(), "schema_index", search_text[:1500], 8)
        except (RuntimeError, OSError, ValueError):
            hits = []
        targets = list(dict.fromkeys(hit["key"] for hit in hits if hit["score"] > 0.20))

    options = []
    for name in targets:
        if not writable_doctype(name):
            continue
        try:
            types = load_mutation_schema(client, name, operation)
        except (FrappeError, MutationFailure):
            continue
        fields = _field_suggestions(question, describe_mutation_fields(name, operation, types))
        options.append(f"- **{name}**" + (f" ({', '.join(fields)})" if fields else ""))
        if len(options) == 3:
            break

    if not options:
        return ("Which kind of record do you want to change? "
                "Tell me the form name or where it appears in Frappe Desk.")

    prompt = "Which DocType did you mean?" if len(options) > 1 else "Did you mean this DocType?"
    return (prompt + "\n\n" + "\n".join(options)
            + "\n\nInclude the name and the details of the change in your next message.")


def _draft_result(question: str, answer: str, history: list[Message], vault: Vault,
                  *, proposal=None) -> AgentResult:
    return AgentResult(answer, [
        *history, Message("user", vault.mask_typed(question)), Message("assistant", answer),
    ], vault=vault, mutation=proposal)


def plan_mutation(question: str, user: UserContext, client: FrappeClient, history: list[Message],
                  vault: Vault, llm: LLMProvider | None) -> AgentResult:
    phone_update = _MY_PHONE_UPDATE.fullmatch(question.strip().removesuffix("\\").rstrip())
    operation = _literal_operation(question)
    targets = ["Employee"] if phone_update else _targets(question)
    trace = current_trace()
    if trace:
        trace.matched_doctypes = targets
    if operation is None or len(targets) != 1:
        if trace:
            trace.error("proposal", "invalid_target")
        answer = (_clarify_target(question, operation, targets, client) if operation else
                  "Which action do you want to take: create, update, submit, cancel, or delete?")
        return _draft_result(question, answer, history, vault)
    doctype = targets[0]
    started = time.monotonic()
    outcome, status = None, None
    try:
        if phone_update:
            request = MutationRequest(
                operation="update", doctype=doctype, name=user.employee_id,
                payload={"cell_number": phone_update["number"].strip()},
            )
            prepared = prepare_mutation(client, user, request)
        else:
            # Keep proposal placeholders scoped to the current request.
            planning_vault = Vault()
            masked = planning_vault.mask_typed(question)
            types = load_mutation_schema(client, doctype, operation)
            schema = describe_mutation_fields(doctype, operation, types)
            guidance = (
                f"Prepare one {operation} proposal for {doctype}. The user's latest message is the only "
                "source for record IDs and field values. Keep placeholders exactly as written. "
                "For an existing record require its exact ID. Use the available fields below. "
                "Ask for missing inputs using a question. If the intended field is unclear, "
                "offer relevant field labels from the schema and ask which one to change. "
                "A proposal never changes Frappe. "
                f"Field schema data: {json.dumps(schema, ensure_ascii=False)}\n{language_hint(question)}"
            )
            reply = (llm or get_llm()).generate(guidance, [Message("user", masked)], [_TOOL])
            if not reply.tool_calls:
                question_for_user = reply.text.strip()
                fields = _field_suggestions(question, schema)
                if question_for_user.endswith("?"):
                    answer = question_for_user
                elif fields:
                    answer = f"Which field on {doctype} should I use ({', '.join(fields)})?"
                elif operation == "create":
                    answer = f"What details should I use to create the {doctype} record?"
                else:
                    answer = f"Which {doctype} record should I use? Please include its record ID."
                return _draft_result(question, answer, history, vault)
            if len(reply.tool_calls) != 1 or reply.tool_calls[0].name != _TOOL["name"]:
                raise MutationFailure(422, "Request one record change at a time.")
            # Reinsert values the caller typed after inference. The confirmation card shows the exact result.
            proposal = _restore(reply.tool_calls[0].args, planning_vault)
            request = MutationRequest.model_validate(proposal)
            if request.operation != operation or request.doctype != doctype:
                raise MutationFailure(422, "The proposed operation or DocType did not match the request. Please restate it.")
            if request.name is not None and not re.search(
                rf"(?<![\w.-]){re.escape(request.name)}(?![\w-]|\.\w)", question,
            ):
                raise MutationFailure(422, "Include the exact record ID in your request.")
            prepared = prepare_mutation(client, user, request, types)
        outcome = "ok"
    except ValidationError:
        outcome, status = "invalid", 422
        return _draft_result(question, "The proposal was invalid. Check the record ID and field values.", history, vault)
    except FrappeError as exc:
        failure = frappe_failure(exc)
        status = failure.status
        outcome = "denied" if status == 403 else "failed"
        return _draft_result(question, str(failure), history, vault)
    except MutationFailure as exc:
        status = exc.status
        outcome = "denied" if status == 403 else "invalid"
        return _draft_result(question, str(exc), history, vault)
    finally:
        if trace and outcome:
            trace.record_tool("propose_mutation", {"doctype": doctype, "operation": operation}, outcome,
                              round((time.monotonic() - started) * 1000, 2), status=status)
    answer = f"Review the proposed change below. Click Confirm {operation} to apply it or Discard to dismiss it."
    return _draft_result(question, answer, history, vault, proposal=prepared)


def _restore(value, vault: Vault):
    if isinstance(value, str):
        if vault.unknown_placeholders(value):
            raise MutationFailure(422, "A proposed value could not be matched to your message.")
        return vault.unmask(value)
    if isinstance(value, list):
        return [_restore(item, vault) for item in value]
    if isinstance(value, dict):
        return {key: _restore(item, vault) for key, item in value.items()}
    return value
