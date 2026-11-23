"""Interpret one chat write request as a proposal. The model has no confirm API."""
import json
import re

from pydantic import ValidationError

from app.core.agent import AgentResult
from app.core.language import language_hint
from app.core.llm import get_llm
from app.core.llm.base import LLMProvider, Message
from app.core.masking import Vault
from app.auth.security import UserContext
from app.core.sites import current_site
from app.frappe_client.client import FrappeClient, FrappeError
from app.indexer.alias_resolver import normalize
from app.knowledge_packs.loader import active_packs
from app.mutations.service import (
    MutationFailure, MutationRequest, describe_mutation_fields, frappe_failure, load_mutation_schema,
    prepare_mutation, writable_doctype,
)

_VERBS = {
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


def _targets(question: str) -> list[str]:
    enabled = {name for name in current_site().write_doctypes if writable_doctype(name)}
    text = f" {normalize(question)} "
    matches = {target for target in enabled if f" {normalize(target)} " in text}
    for pack in active_packs():
        for alias, targets in pack.aliases.items():
            if f" {normalize(alias)} " in text:
                matches.update(enabled.intersection(targets if isinstance(targets, tuple) else (targets,)))
    return sorted(matches)


def _literal_operation(question: str) -> str | None:
    patterns = {name: re.search(rf"\b(?:{pattern})\b", question, re.IGNORECASE)
                for name, pattern in _VERBS.items()}
    matches = [(match.start(), name) for name, match in patterns.items() if match]
    return min(matches)[1] if matches else None


def _draft_result(question: str, answer: str, history: list[Message], vault: Vault,
                  *, proposal=None) -> AgentResult:
    return AgentResult(answer, [
        *history, Message("user", vault.mask_typed(question)), Message("assistant", answer),
    ], vault=vault, mutation=proposal)


def plan_mutation(question: str, user: UserContext, client: FrappeClient, history: list[Message],
                  vault: Vault, llm: LLMProvider | None) -> AgentResult:
    operation = _literal_operation(question)
    targets = _targets(question)
    if operation is None or len(targets) != 1:
        return _draft_result(question,
                             "Ask for one create, update, submit, cancel, or delete and name a DocType enabled for chat writes.",
                             history, vault)
    doctype = targets[0]
    # Use a fresh vault so the proposed payload cannot refer to values from previous chat turns.
    planning_vault = Vault()
    masked = planning_vault.mask_typed(question)
    try:
        types = load_mutation_schema(client, doctype, operation)
        schema = describe_mutation_fields(doctype, operation, types)
        guidance = (
            f"Prepare one {operation} proposal for {doctype}. The user's latest message is the only "
            "source for record IDs and field values. Keep placeholders exactly as written. "
            "For an existing record require its exact ID. Use the available fields below. "
            "Ask for missing inputs using a question. A proposal never changes Frappe. "
            f"Field schema data: {json.dumps(schema, ensure_ascii=False)}\n{language_hint(question)}"
        )
        reply = (llm or get_llm()).generate(guidance, [Message("user", masked)], [_TOOL])
        if not reply.tool_calls:
            question_for_user = reply.text.strip()
            answer = question_for_user if question_for_user.endswith("?") else (
                "Specify the record ID and field values needed for this change."
            )
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
    except ValidationError:
        return _draft_result(question, "The proposal was invalid. Check the record ID and field values.", history, vault)
    except FrappeError as exc:
        return _draft_result(question, str(frappe_failure(exc)), history, vault)
    except MutationFailure as exc:
        return _draft_result(question, str(exc), history, vault)
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
