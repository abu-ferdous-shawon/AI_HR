"""One-time confirmation tokens bound to the Frappe login and conversation."""
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from app.auth.credentials import Credentials, current_holder
from app.auth.security import UserContext
from app.core.sites import current_site
from app.frappe_client.client import FrappeClient, FrappeError
from app.mutations.service import MutationFailure, PreparedMutation, frappe_failure, prepare_mutation

TTL_SECONDS = 300
MAX_PENDING = 256


@dataclass(frozen=True)
class Binding:
    site: str
    user: str
    conversation_id: str
    session: str


@dataclass(frozen=True)
class Pending:
    binding: Binding
    change: PreparedMutation
    expires: float
    expires_at: str

    def card(self, token: str) -> dict:
        request = self.change.request
        return {
            "token": token, "operation": request.operation, "doctype": request.doctype,
            "document_name": request.name, "changes": list(self.change.changes),
            "expires_at": self.expires_at,
        }


class ConfirmationStore:
    def __init__(self):
        self._pending: dict[str, Pending] = {}
        self._lock = Lock()

    def issue(self, binding: Binding, change: PreparedMutation) -> dict:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        expires_at = datetime.fromtimestamp(time.time() + TTL_SECONDS, timezone.utc).isoformat()
        pending = Pending(binding, change, now + TTL_SECONDS, expires_at)
        with self._lock:
            self._pending = {
                key: entry for key, entry in self._pending.items()
                if entry.expires > now and (entry.binding.site, entry.binding.user, entry.binding.conversation_id)
                != (binding.site, binding.user, binding.conversation_id)
            }
            if len(self._pending) >= MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[token] = pending
        return pending.card(token)

    def pending_card(self, binding: Binding) -> dict | None:
        now = time.monotonic()
        with self._lock:
            for token, pending in self._pending.items():
                if pending.binding == binding and pending.expires > now:
                    return pending.card(token)
        return None

    def take(self, binding: Binding, token: str) -> PreparedMutation:
        with self._lock:
            pending = self._pending.get(token)
            if pending is not None and pending.expires <= time.monotonic():
                self._pending.pop(token)
                pending = None
            if pending is None or pending.binding != binding:
                raise MutationFailure(409, "This confirmation is unavailable. Request a new proposal.")
            return self._pending.pop(token).change


confirmations = ConfirmationStore()


def bind(user: UserContext, conversation_id: str, credentials: Credentials | None) -> Binding:
    auth = (credentials.authorization or credentials.sid) if credentials else None
    if not auth:
        raise MutationFailure(403, "Sign in with your own Frappe account to confirm a change.")
    site = current_site()
    auth_type = "authorization" if credentials.authorization else "sid"
    identity = hashlib.sha256(f"{site.frappe_url}\0{auth_type}\0{auth}".encode()).hexdigest()
    return Binding(site.name, user.user_id, conversation_id, identity)


def execute_mutation(confirmation_token: str, user: UserContext, conversation_id: str,
                     client: FrappeClient, *, discard: bool = False) -> dict:
    holder = current_holder()
    if holder is None or holder.client is not client or not client.acts_as_user:
        raise MutationFailure(403, "Sign in with your own Frappe account to confirm a change.")

    entry = confirmations.take(bind(user, conversation_id, holder.credentials), confirmation_token)
    request = entry.request
    if discard:
        return {"state": "discarded", "operation": request.operation, "doctype": request.doctype,
                "document_name": request.name, "answer": "Change discarded."}
    try:
        latest = prepare_mutation(client, user, request)
    except FrappeError as exc:
        raise frappe_failure(exc) from None
    if latest.modified != entry.modified or latest.payload != entry.payload or latest.changes != entry.changes:
        raise MutationFailure(409, "The record or its schema changed after review. Request a new proposal.")

    name = request.name
    try:
        if request.operation == "create":
            name = client.create_document(request.doctype, entry.payload).get("name")
        elif request.operation == "delete":
            client.delete_document(request.doctype, name)
        else:
            client.update_document(request.doctype, name, entry.payload, modified=entry.modified)
    except FrappeError as exc:
        raise frappe_failure(exc, dispatched=True) from None

    operation = {"create": "Created", "update": "Updated", "submit": "Submitted",
                 "cancel": "Cancelled", "delete": "Deleted"}[request.operation]
    record = f" {name}" if isinstance(name, str) and name else ""
    return {"state": "executed", "operation": request.operation, "doctype": request.doctype,
            "document_name": name, "answer": f"{operation} {request.doctype}{record}."}
