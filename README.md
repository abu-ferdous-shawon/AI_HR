# Frappe AI Assistant

This backend connects a chat UI to Frappe v16, ERPNext, and HRMS. It can answer
policy questions, query permitted records and reports, and propose changes to
explicitly enabled DocTypes for user confirmation.

## Local setup

Install `requirements.txt` in a virtual environment. Copy `.env.example` to
`.env` and configure the Frappe and Gemini credentials. The Frappe integration
login supports identity and role lookups. In `user_session` mode each chat
user signs in with their own Frappe token or Desk session and needs an active
Employee record linked to that login. The [installation runbook](deploy/INSTALL.md#phase-1-local-verification)
covers encrypted secrets, multisite configuration, and permission checks.

```bash
DEV_MODE=false FRAPPE_AUTH_MODE=user_session \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Open `http://localhost:8100/` and sign in as the Frappe user whose access you want
to exercise. The runbook also covers the [Desk widget](deploy/INSTALL.md#4-frappe-widget-app)
and [confirmed writes](deploy/INSTALL.md#phase-5-confirmed-writes).

## Metadata and knowledge packs

Build the four metadata indexes using a credential with read access to the
site’s metadata catalog. Use the same site, embedding provider, index path,
and active knowledge packs when running chat. See the [indexing steps](deploy/INSTALL.md#phase-2-metadata-indexing).

```bash
.venv/bin/python -m app.indexer sync_metadata
.venv/bin/python -m app.indexer status
```

Start with [the example YAML pack](app/knowledge_packs/custom_app_example.yaml)
to add your site's DocType aliases, statuses, terminology, report hints, or
`never_send` field exclusions. Set `KNOWLEDGE_PACK_DIR` and `KNOWLEDGE_PACKS`
for the indexer and chat, then sync metadata. The [knowledge pack runbook](deploy/INSTALL.md#phase-3-knowledge-packs)
includes a local example and redaction checks.

## Query correction and diagnostics

Correctable read errors can trigger up to two model correction attempts.
To inspect your own request in the UI, sign in as a Frappe System Manager
and enable **Debug** before sending a message. See the
[Phase 6 local checks](deploy/INSTALL.md#phase-6-query-correction-and-chat-diagnostics)
for what to look for in the response and audit log.
