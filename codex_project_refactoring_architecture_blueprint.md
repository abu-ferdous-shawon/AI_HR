# codex Engineering Specification: Upgrading `AI_HR` into a Universal, Permission-Aware Frappe v16 AI Assistant

## 1. Mission & Architectural North Star

You are tasked with refactoring and upgrading this repository (`AI_HR`) from a domain-specific HR chatbot into a **universal, plug-and-play, permission-aware AI assistant** for **Frappe v16.55.1 / ERPNext v16.53.0 / HRMS v16.53.0** and any installed custom Frappe apps.

This upgrade combines:

1. **Dynamic Tool-Calling over Frappe ORM/REST/RPC APIs** (preserving Frappe's native RBAC, User Permissions, field `permlevel`, workflows, and hooks).

2. **Proven RAG, Schema Pruning, Knowledge Pack, and Guardrail Patterns** distilled from the **ChangAI** architecture—while strictly avoiding ChangAI's static snapshots and raw SQL execution anti-patterns.

## 2. Phase 0: Mandatory Repository Discovery (Execute First)

Before modifying or creating any files, scan the entire workspace and produce an internal map of:

1. **Entry Points & Server Framework**: Locate the main web server / API routes (e.g., FastAPI, Flask, Frappe whitelist endpoints, or Node/Python agent runner).

2. **Frappe Connection Layer**: Identify where `FRAPPE_URL`, `API_KEY`, `API_SECRET`, or session tokens are configured and how HTTP/RPC calls are dispatched to Frappe.

3. **LLM & Embedding Clients**: Locate model provider wrappers, tool/function definitions, system prompts, and conversation memory storage.

4. **Frontend / Chat UI**: Identify the chat interface components, state management, and payload contracts.

5. **Existing HR-Specific Logic**: Flag every hardcoded reference to `Employee`, `Attendance`, `Leave Application`, `Salary Slip`, or HR-only prompt instructions.

## 3. Architectural Rules: What to Drop vs. What to Adopt

### 3.1 Strictly DROP / Remove from `AI_HR`

1. **DROP Single Global Admin API Key Execution for User Queries**:

   * *Reason*: Executing end-user prompts with a single System Manager `api_key:api_secret` bypasses Frappe's Role Permissions and User Permissions, exposing confidential payroll, HR, and accounting records across users.

   * *Replacement*: Authenticate every request using the **active logged-in user's session/OAuth token** (or a Frappe-side proxy endpoint executing strictly under `frappe.session.user`). Reserve service/admin credentials *only* for background metadata indexing jobs.

2. **DROP Hardcoded HR-Only Prompts, Routers, and DocType Lists**:

   * *Reason*: Hardcoding HR DocTypes prevents the assistant from working seamlessly across Accounting, Stock, Selling, Buying, Manufacturing, CRM, and custom Frappe apps.

   * *Replacement*: Dynamic DocType discovery via live `frappe.get_meta` introspection, auto-synced vector indexes, and declarative YAML Knowledge Packs.

3. **DROP Raw Unfiltered `get_meta` Dumps into LLM Context**:

   * *Reason*: Raw Frappe `DocType` metadata includes layout breaks (`Section Break`, `Column Break`, `Tab Break`, `HTML`), internal flags, and hundreds of irrelevant fields that waste tokens and induce tool-call hallucinations.

   * *Replacement*: Strip UI/layout fields during indexing and perform two-stage schema retrieval + field pruning per query.

4. **NEVER Use Raw Text-to-SQL Execution**:

   * *Reason*: Direct SQL queries bypass `has_permission`, `permission_query_conditions`, `docstatus` rules, and encrypted fields. All data reads and writes **must** pass through Frappe's permission-checked APIs (`frappe.client.get_list`, `frappe.client.get`, `frappe.desk.query_report.run`, `frappe.client.insert`, `frappe.client.set_value`, `frappe.client.delete`).

## 4. Target System Architecture & Implementation Phases

### Phase 1: Permission-Aware Frappe Client & Auth Layer

Refactor the Frappe client connector so every user-initiated tool call enforces native Frappe permissions:

1. **Per-User Authentication Context**:

   * Accept the caller's Frappe session cookie (`sid`), Bearer/OAuth token, or user-scoped API key/secret on every chat request.

   * Forward the user's credentials on all Frappe REST/RPC calls so Frappe evaluates `Role Permissions`, `User Permissions` (Company, Department, Employee, Territory, etc.), and `permlevel` natively.

   * Never cache query results across different users unless keyed by `(user_id, permissions_hash)`.

2. **Credential & Configuration Management**:

   * Support multi-site / plug-and-play connection configuration (Frappe Base URL, LLM Provider/Model, Embedding Provider/Model, Active Knowledge Packs, and Indexing Schedule) via environment variables and/or a `Chatbot Settings` configuration store.

   * Ensure stored API keys are encrypted at rest.

### Phase 2: Auto-Synced Four-Tier Schema & Report Indexing Engine

Build a dynamic metadata indexer (`indexer/`) that connects to the target Frappe v16 site, inspects all enabled modules/DocTypes/Reports, and builds four lightweight vector stores (using FAISS or Chroma) plus deterministic lookup maps:

1. **Metadata Extraction & Cleaning (`schema_extractor.py`)**:

   * Fetch all non-single/single DocTypes accessible on the site (`frappe.get_meta`), excluding system internals (`DocType`, `DocField`, `Version`, `Activity Log`, `Error Log`, etc., unless explicitly enabled).

   * Strip layout-only fieldtypes: `Section Break`, `Column Break`, `Tab Break`, `HTML`, `Fold`, `Heading`, `Button`.

   * Extract per DocType: `name`, `module`, `description`, `is_submittable`, `istable` (child table), `title_field`, `search_fields`, `status` options, linked DocTypes (`Link` / `Dynamic Link` / `Table`), and cleaned field definitions (`fieldname`, `label`, `fieldtype`, `options`, `reqd`, `in_list_view`, `in_standard_filter`).

2. **Four-Tier Vector Stores (`vector_store.py`)**:

   * **Store 1 — `table_index`**: Compact summaries of each DocType (Name, Module, Purpose, Key Linked Entities) for fast top-$K$ DocType candidate selection.

   * **Store 2 — `schema_index`**: Detailed DocType schema cards including child tables, workflow states, and relationships.

   * **Store 3 — `field_index`**: Field-level embeddings (`DocType.fieldname` + label + options) to prune large DocTypes (e.g., `Employee`, `Sales Invoice`) down to only the fields relevant to the user's question.

   * **Store 4 — `report_index`**: All enabled Frappe Script Reports and Query Reports (`Report` DocType), indexing report name, `ref_doctype`, module, description, and expected filter fields.

3. **Business Alias & Keyword Resolver (`alias_resolver.py`)**:

   * Auto-generate `alias_map.json` and `business_keywords.json` from DocType labels, plural forms, `search_fields`, and loaded Knowledge Packs (e.g., mapping `"pto"` / `"time off"` $\rightarrow$ `Leave Application`, `"payslip"` $\rightarrow$ `Salary Slip`, `"customer invoice"` $\rightarrow$ `Sales Invoice`).

4. **Incremental & On-Demand Sync**:

   * Expose a `sync_metadata` admin endpoint/CLI command and a lightweight schema hash check so adding a custom DocType, Custom Field, or custom Frappe app automatically updates the indexes without code changes.

### Phase 3: Declarative YAML Knowledge Pack Engine

Implement a pluggable Knowledge Pack loader (`knowledge_packs/`) that reads `.yaml` files from a `knowledge_packs/` directory (with built-in starter packs for `hrms`, `erpnext_core`, and custom apps). Every Knowledge Pack must support five structured sections:

```
pack_name: "hrms_core"
version: "1.0"
modules: ["HR", "Payroll"]

# 1. Overview, Business Flows & Supported Topics
overview:
  description: "Manages employee lifecycle, attendance, leave requests, shifts, and payroll processing."
  flows:
    - "Leave Request Flow: Leave Application (Open -> Approved/Rejected) deducts from Leave Allocation."
    - "Payroll Flow: Salary Structure Assignment -> Payroll Entry -> Salary Slip (Draft -> Submitted)."

# 2. DocTypes, Status Fields & Workflow States
doctypes:
  Leave Application:
    status_field: "status"
    states: ["Open", "Approved", "Rejected", "Cancelled"]
    date_fields: ["from_date", "to_date", "posting_date"]
    owner_link: "employee"
  Salary Slip:
    status_field: "docstatus"
    states: {"0": "Draft", "1": "Submitted", "2": "Cancelled"}
    owner_link: "employee"

# 3. Hard Exclusion Guardrails (NEVER send to LLM or User)
never_send:
  global_fields: ["password", "api_secret", "auth_token", "user_secret"]
  doctype_fields:
    Employee: ["bank_ac_no", "iban", "pan_number", "ssn", "passport_number", "health_details"]
    Salary Slip: ["bank_account_no"]

# 4. Domain Vocabulary, KPIs & Standard Reports
vocabulary:
  "on leave today": "Leave Application where status='Approved' and from_date <= today <= to_date"
  "headcount": "Count of Employee where status='Active'"
recommended_reports:
  - name: "Monthly Attendance Sheet"
    when_to_use: "Monthly attendance summary, present/absent counts per employee"
  - name: "Salary Register"
    when_to_use: "Aggregated salary components, gross pay, deductions, and net pay"

# 5. Master Hints & Few-Shot Examples
examples:
  - question: "Who in Engineering is on leave this week?"
    intent: "query_records"
    target_doctype: "Leave Application"
    notes: "Filter status='Approved', department='Engineering', overlapping date range."

```

#### Mandatory `never_send` Enforcement Rules:

* Strip all `never_send` fields from schema cards before sending metadata to the LLM.

* Automatically exclude `never_send` fields from `fields=[...]` in `get_list` / `get_doc` calls.

* Run a post-execution response sanitizer that redacts any `never_send` keys if returned by a report or nested child table.

### Phase 4: Intent Router & Universal Tool-Calling Pipeline

Replace single-pass prompting with a structured multi-stage agent orchestrator (`agent/`):

1. **Stage 1 — Intent Fast-Path Router (`intent_router.py`)**:

   * Classify incoming messages into:

     * `conversational`: Greetings, thanks, capability questions, or general policy Q&A $\rightarrow$ respond immediately without schema retrieval or Frappe API calls.

     * `erp_read`: Analytical questions, record lookups, counts, status checks, or report requests.

     * `erp_write`: Requests to create, update, submit, cancel, or delete records.

2. **Stage 2 — Contextual Query Rewriting (`query_rewriter.py`)**:

   * Resolve coreferences from chat history (e.g., turning *"What about last month?"* into *"Show approved Leave Applications for \[User/Department\] in October 2026"*), injecting current user identity (`user`, `employee_id`, `company`, `today_date`) from the Frappe session.

3. **Stage 3 — Two-Stage Schema & Report Retrieval (`retriever.py`)**:

   * Resolve business aliases via `alias_resolver.py`.

   * Query `table_index` + `report_index` to retrieve top-$K$ candidate DocTypes and Reports.

   * Query `field_index` and apply an LLM/heuristic field pruner to retain only the fields required to filter, group, and answer the query.

   * Inject matching Knowledge Pack rules, status definitions, and few-shot examples.

4. **Stage 4 — Universal Frappe Tool Suite (`tools/`)**:
   Expose a clean, strictly typed tool interface to the LLM:

   * `search_schema(query: str)`: Retrieves pruned DocType fields, link relationships, and valid status options.

   * `query_records(doctype: str, fields: list, filters: list, or_filters: list, order_by: str, limit: int)`: Executes permission-scoped `frappe.client.get_list` (enforcing `never_send` redaction and sensible pagination limits).

   * `get_record_detail(doctype: str, name: str)`: Fetches a single document including permitted child tables via `frappe.client.get`, sanitized against `never_send`.

   * `aggregate_records(doctype: str, group_by: str, aggregate_fn: str, aggregate_field: str, filters: list)`: Performs safe ORM-level `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` groupings without raw SQL.

   * `run_frappe_report(report_name: str, filters: dict)`: Executes standard Frappe Script/Query Reports (`frappe.desk.query_report.run`) when a report in `report_index` matches the analytical intent.

   * `propose_mutation(operation: Literal["create", "update", "submit", "cancel", "delete"], doctype: str, name: Optional[str], payload: dict)`: Validates fields against pruned schema and returns a human-readable confirmation card without executing yet.

   * `execute_mutation(confirmation_token: str)`: Executes the approved mutation via Frappe's document API (`insert`, `set_value`, `submit`, `cancel`, `delete`) under the user's session.

### Phase 5: Safe Write Operations (CUD) & Confirmation Gate

All state-changing actions (`create`, `update`, `submit`, `cancel`, `delete`) must follow a two-step **Propose** $\rightarrow$ **Confirm** $\rightarrow$ **Execute** workflow:

1. Validate target DocType, required fields (`reqd=1`), fieldtypes, and `Link` target existence before proposing.

2. Return a structured confirmation payload to the UI detailing the exact operation, target DocType, record ID (if updating/deleting), and field changes.

3. Execute the Frappe API write call **only** after explicit user confirmation is received.

4. Surface Frappe validation errors (`ValidationError`, `PermissionError`, `MandatoryError`) cleanly to the user.

### Phase 6: Self-Correcting Retry Loop & Observability

1. **Bounded Error Retry Loop (`retry_handler.py`)**:

   * If `query_records`, `aggregate_records`, or `run_frappe_report` fails due to a schema/filter syntax error (e.g., invalid fieldname, wrong date format, missing mandatory report filter), feed the exact Frappe error message and pruned schema back into the LLM for up to **2 self-correction attempts**.

   * Do **not** retry on `PermissionError` (403); immediately inform the user that they lack Frappe permissions for that DocType or record.

2. **Structured Execution Logs & Debug Payload**:

   * Log every turn (anonymizing `never_send` fields) with: `session_id`, `user`, `rewritten_query`, `matched_doctypes`, `matched_reports`, `tool_calls`, `retry_count`, `latency_ms`, and `error_trace`.

   * Include an optional `debug` metadata object in API responses (toggleable in the UI) so developers/admins can inspect retrieved schemas, tool arguments, and execution times.

## 5. Directory Structure Target for `AI_HR`

Organize the refactored codebase cleanly into modular packages:

```
AI_HR/
├── app/
│   ├── main.py                      # API server & health/sync endpoints
│   ├── config.py                    # Multi-site & provider settings
│   ├── auth/
│   │   └── frappe_session.py        # Per-user sid / OAuth / token resolution
│   ├── frappe_client/
│   │   ├── client.py                # Permission-scoped REST/RPC wrapper
│   │   └── schema_extractor.py      # Live get_meta & report introspection
│   ├── indexer/
│   │   ├── vector_store.py          # 4-tier FAISS/Chroma index builder & search
│   │   └── alias_resolver.py        # Business synonym & keyword mapper
│   ├── knowledge_packs/
│   │   ├── loader.py                # YAML loader, validator & never_send redactor
│   │   ├── hrms_core.yaml           # HRMS v16 domain pack
│   │   └── erpnext_core.yaml        # ERPNext v16 core pack
│   ├── agent/
│   │   ├── orchestrator.py          # End-to-end pipeline coordinator
│   │   ├── intent_router.py         # Conversational vs. Read vs. Write router
│   │   ├── query_rewriter.py        # Multi-turn & user-context resolver
│   │   ├── retriever.py             # Two-stage schema & report retrieval
│   │   └── retry_handler.py         # Bounded self-correcting error recovery
│   ├── tools/
│   │   ├── read_tools.py            # search_schema, query_records, get_detail, aggregate
│   │   ├── report_tools.py          # run_frappe_report
│   │   └── mutation_tools.py        # propose_mutation & execute_mutation
│   └── prompts/
│       ├── system_prompt.txt
│       ├── rewrite_prompt.txt
│       ├── filter_fields_prompt.txt
│       ├── mutation_prompt.txt
│       └── retry_prompt.txt
├── frontend/                        # Chat UI (with confirmation cards & debug drawer)
├── tests/
│   ├── test_permissions.py
│   ├── test_never_send.py
│   ├── test_retriever.py
│   ├── test_tools_and_retry.py
│   └── test_mutations.py
└── README.md

```

## 6. Verification & Acceptance Checklist (Must Pass Before Completion)

codex must verify every item below before concluding the refactor:

* \[ \] **Zero Global Admin Queries**: No user-facing query or mutation executes with a shared System Manager API key; all Frappe calls run under the requesting user's session/token.

* \[ \] **Permission Isolation Verified**: User A (Employee) cannot retrieve User B's `Salary Slip`, `Leave Application`, or restricted `Employee` records; Frappe's `PermissionError` or row filtering is respected.

* \[ \] **`never_send` Redaction Verified**: Fields listed in a Knowledge Pack's `never_send` block never appear in LLM prompts, tool outputs, logs, or final chat responses.

* \[ \] **Zero Hardcoded HR Logic**: Asking questions across `HRMS` (`Leave Application`, `Attendance`), `ERPNext` (`Sales Order`, `Item`, `Purchase Invoice`), and any custom DocType works dynamically via schema retrieval without code edits.

* \[ \] **Report Execution Works**: Analytical queries matching an indexed Frappe Report (e.g., *Monthly Attendance Sheet*) invoke `run_frappe_report` instead of raw record pagination.

* \[ \] **Two-Step CUD Confirmation Works**: Create/Update/Submit/Cancel/Delete requests return a structured confirmation card first and only mutate Frappe data upon explicit user confirmation.

* \[ \] **Self-Correcting Retry Works**: Simulated invalid filter or field errors trigger automatic recovery within 2 retries and return a valid response.

* \[ \] **Documentation Updated**: `README.md` documents installation, per-user authentication setup, metadata indexing, and authoring custom YAML Knowledge Packs.