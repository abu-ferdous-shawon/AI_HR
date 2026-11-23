# HR AI Assistant: VM installation

Target: the Linux VM that runs Frappe HRMS v16 behind nginx.
Needs: sudo, Python 3.11+, outbound HTTPS to the LLM provider only.

## 0. Information to collect first
| Item | Example | Who gives it |
|---|---|---|
| Site name (bench) | hrm.example.com | Frappe admin |
| Bench path | /home/frappe/frappe-bench | Frappe admin |
| Site nginx file | /etc/nginx/conf.d/frappe-bench.conf | `bench setup nginx` output |
| Frappe API key + secret of the "AI Integration" user | (secret) | Frappe admin |
| LLM key | (secret) | Project owner |

## 1. Frappe side (once)
1. Create a User "ai-integration@yourcompany.com". Do NOT give System Manager.
   Give read-only access to the HR doctypes in the registry profile (Employee, Leave Application, Attendance, Salary Slip and the rest).
2. Open that user, Settings > API Access > Generate Keys. Save key and secret.
3. Every staff member who will use the chat needs a Frappe User with the Employee role,
   linked from Employee > User ID. Staff without a login cannot use the assistant (by design).
4. Managers: "Reports To" must be set on their team's Employee records.
5. HR staff: role "HR Manager" or "HR User".

## 2. Install the backend (run from the project folder copied to the VM)
```
sudo useradd --system --home /opt/hr-ai --shell /usr/sbin/nologin hrai
sudo mkdir -p /opt/hr-ai
sudo rsync -a --exclude .venv --exclude .env --exclude __pycache__ ./ /opt/hr-ai/
sudo chown -R root:hrai /opt/hr-ai && sudo chmod -R g+rX,o-rwx /opt/hr-ai
sudo python3 -m venv /opt/hr-ai/.venv
sudo /opt/hr-ai/.venv/bin/pip install -r /opt/hr-ai/requirements.txt
sudo install -d -m 750 -o root -g hrai /etc/hr-ai
sudo install -m 600 -o root -g root deploy/hr-ai.env.example /etc/hr-ai/hr-ai.env
sudo nano /etc/hr-ai/hr-ai.env
```
Fill in: FRAPPE_URL (the public https URL of the site), FRAPPE_API_KEY, FRAPPE_API_SECRET,
GEMINI_API_KEY. Keep ENVIRONMENT=production and DEV_MODE=false.
If the VM cannot resolve the site's public name, add it to /etc/hosts pointing at 127.0.0.1.

Copy the policy documents (PDF/DOCX/MD) into /opt/hr-ai/policies/.

```
sudo cp deploy/hr-ai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hr-ai
sudo systemctl status hr-ai --no-pager
curl -s http://127.0.0.1:8100/health        # {"status":"ok"}
```

## 3. nginx
1. Open the site's nginx file.
2. Paste the `location /ai/ { ... }` block from deploy/nginx_frappe_ai.conf inside the
   `server { ... }` block, above `location /`.
3. Test and reload:
```
sudo nginx -t && sudo systemctl reload nginx
```
4. Check: `curl -s https://YOUR_SITE/ai/health` returns {"status":"ok"}.

## 4. Frappe widget app
```
cd /home/frappe/frappe-bench
bench get-app /opt/hr-ai/frappe_app/hr_ai_widget
bench --site YOUR_SITE install-app hr_ai_widget
bench build --app hr_ai_widget
bench --site YOUR_SITE clear-cache
bench restart
```
If apps cannot be installed: skip this, and enable the three commented `sub_filter`
lines from deploy/nginx_frappe_ai.conf instead, then reload nginx.

## 5. Acceptance checks
| # | Check | Expected |
|---|---|---|
| 1 | `sudo ss -ltnp \| grep 8100` | only 127.0.0.1:8100 |
| 2 | `curl -s -o /dev/null -w "%{http_code}" https://YOUR_SITE/ai/docs` | 404 |
| 3 | POST /ai/chat with header `Origin: https://evil.example` | 403 |
| 4 | `sudo stat -c '%a %U' /etc/hr-ai/hr-ai.env` | 600 root |
| 5 | `sudo tail -n 3 /var/log/hr-ai/app.log` | JSON lines |
| 6 | Set DEV_MODE=true, restart | service refuses to start |
| 7 | Log in as an employee, open /app | 💬 button visible; not on /login |
| 8 | Employee asks "How many casual leaves do I have left?" | correct number |
| 9 | Employee asks for company payroll cost | "HR only" |
| 10 | HR user asks the same | figure returned |
| 11 | Manager asks for team attendance | works; employee gets refused |
| 12 | `sudo systemctl stop hr-ai`, send a message | "Cannot reach the assistant" |

## 6. Updating later
```
sudo rsync -a --exclude .venv --exclude .env --exclude __pycache__ ./ /opt/hr-ai/
sudo systemctl restart hr-ai
```
The widget script updates itself (served by the backend). Only changes under
frappe_app/ need `bench build` again.

## 7. Rollback
`bench --site YOUR_SITE uninstall-app hr_ai_widget` removes the button.
`sudo systemctl disable --now hr-ai` stops the backend. Remove the `location /ai/` block and reload nginx.
## Phase 1 local verification

Start the backend from the repository root using the same environment that
can decrypt the configured secrets.

```bash
.venv/bin/python -m app.core.secret_store check
DEV_MODE=false FRAPPE_AUTH_MODE=user_session .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Open `http://localhost:8100/`. Sign in with a Frappe user token and send
“Show my profile.” Repeat in a separate browser profile with another Frappe
user and verify that each sees their own profile. For a restricted record
check, use a record type available through the chat tools and confirm each
user's access in Frappe Desk first. Each chat user needs exactly one active
Employee linked to their Frappe login.

For multiple sites, copy the structure in `sites.yaml` and set
`SITES_FILE` to the configuration file. Use Frappe URLs for your actual sites.
The browser's hostname chooses the site. Unlisted hostnames use the default
site. Add the variables named by `api_key_env`, `api_secret_env` and optional
`gemini_api_key_env` to the process environment or `.env`. Encrypt their
values with `.venv/bin/python -m app.core.secret_store encrypt`. The metadata
indexer uses the site's embedding, pack and schedule settings.

### Live permission isolation test

In a test Frappe site, prepare two non-admin users and two documents of the
same DocType. Grant each user read access to one document and confirm that
the other document is hidden in both list and direct document views.
Export `FRAPPE_ISOLATION_DOCTYPE` and these variables for each of A and B:

- `FRAPPE_ISOLATION_USER_A` and `FRAPPE_ISOLATION_USER_B` hold the Frappe logins.
- `FRAPPE_ISOLATION_RECORD_A` and `FRAPPE_ISOLATION_RECORD_B` hold the document names.
- `FRAPPE_ISOLATION_TOKEN_A` and `FRAPPE_ISOLATION_TOKEN_B` hold the full authorization headers.
  Load them into your test shell without saving the tokens in shell history.

Set `FRAPPE_ISOLATION_SITE` when testing a configured site other than the
default. Then run:

```bash
.venv/bin/python -m pytest -o addopts='' -m integration -rs tests/test_frappe_permission_isolation.py
```

## Phase 2 metadata indexing

The indexer runs as a local admin command. Run it from the repository root
with access to the site's Frappe URL. The indexing account needs Frappe read
access to `DocType`, `Module Def`, `Custom Field`, `Property Setter`, `Workflow`
and `Report`. Its permissions on the report and referenced DocType also
determine whether Frappe supplies the report's filter script. If you use a
dedicated indexing credential, supply it only to the indexer process. For a
single site, override `FRAPPE_API_KEY` and `FRAPPE_API_SECRET` in that process.
For multiple sites, point the process at an indexing-only `SITES_FILE` with
the appropriate encrypted credential references.

### Build and inspect an index

```bash
.venv/bin/python -m pip install -r requirements.txt
export EMBEDDING_PROVIDER=local
.venv/bin/python -m app.indexer sync_metadata
.venv/bin/python -m app.indexer status
.venv/bin/python -m app.indexer search table_index "time off"
.venv/bin/python -m app.indexer search schema_index "leave application" --limit 1
.venv/bin/python -m app.indexer search field_index "status" --doctype "Leave Application" --limit 1
.venv/bin/python -m app.indexer search report_index "monthly attendance sheet" --limit 1
```

Use names present on your Frappe site. With multiple sites, add
`--site main` before the command and set `embedding_provider: local` in
that site's YAML when it overrides the environment default. Keep the same
embedding settings for sync and search. The local provider uses lexical
similarity and makes no embedding API calls. For Gemini semantic search,
use `embedding_provider: gemini`, the site's embedding model, and its
configured Gemini key. A first sync embeds every card and can use significant
Gemini quota on a large site.

The `status` output shows the active snapshot directory and card counts.
It contains four pairs of `.faiss` and `.json` files plus `alias_map.json`
and `business_keywords.json`. Set `METADATA_INDEX_DIR` to change the output
root. An empty tier has a zero card count. Report cards record whether
filters came from the Report form, static JavaScript declarations, or
were unavailable. Filters computed dynamically in JavaScript cannot be
inferred statically.

### Verify a change from the Frappe UI

1. Run `sync_metadata --check` with the same site and embedding settings.
   Expect `"state": "current"` and exit code 0 after a successful sync.
2. In Frappe Desk on a test site, open **Customize Form**, pick a DocType,
   and add a temporary Data field named **Phase Two Index Check**. Save it
   and note the actual fieldname Frappe assigns.
3. Run `sync_metadata --check` again. Expect `"state": "stale"` and exit code 1.
   Run `sync_metadata` and check that `new_embeddings.field_index` is smaller
   than the full index. Search `field_index` for `"Phase Two Index Check"`
   scoped with `--doctype` to verify the new field.
4. Repeat `sync_metadata` without editing metadata. Expect
   `"state": "unchanged"`.

For scheduled sync, set `INDEXING_SCHEDULE=every:1` or set the site's
`indexing_schedule: every:1`, then run `.venv/bin/python -m app.indexer watch`
as a separate process. It syncs on startup and every configured interval
thereafter. `manual` sites are skipped by the watcher. After changing
indexing permissions or only a report's JavaScript file, run
`sync_metadata --force` to reread metadata even if revision hashes match.

Add selected custom alias packs by setting `KNOWLEDGE_PACK_DIR` and listing
their names in `KNOWLEDGE_PACKS` or the site's `knowledge_packs`. Each pack
file is named `<name>.yaml` and declares `pack_name: <name>`. See
`app/knowledge_packs/hrms_core.yaml` for a complete pack. Use `index_include_doctypes` for specific built-in
exclusions you want to include. Use `index_exclude_doctypes` for site-specific
exclusions. The environment equivalents accept comma-separated names.

Phase 4 uses a compatible local snapshot for permission-scoped chat retrieval.
The sync command remains an indexing-account operation.

## Phase 3 knowledge packs

The default site loads `hrms_core` and `erpnext_core`. The
`app/knowledge_packs/custom_app_example.yaml` starter shows how to describe
a custom app. Copy it into your pack directory, change `pack_name` to
match the filename, and replace the sample modules, DocTypes and fields.
Packs can define business flows and topics, status and ownership fields,
`never_send` rules, vocabulary and KPIs, recommended reports, master hints,
and worked examples. The Phase 4 read path uses matching rules and guidance during retrieval.

Select enabled packs with `KNOWLEDGE_PACKS` for a single-site setup or
`knowledge_packs` in the site's YAML. Set `KNOWLEDGE_PACK_DIR` to the folder
with your custom files. Files in that folder take precedence over built-ins
with the same name. Include the built-in pack names in the enabled list if
you want their policies too. Restart the local backend after editing packs.
Startup reports the enabled names and rejects malformed YAML, misspelled
keys, or unavailable packs.

`never_send.global_fields` applies across DocTypes.
`never_send.doctype_fields` uses the exact DocType name and the underlying
fieldnames. A document or report column that is excluded cannot be restored
through the confidential-value masking feature. List requests omit blocked
fields and reject filters and sorting on them. Frappe's document endpoint
returns a permission-checked document, including child rows. The backend
removes blocked keys immediately on receipt. Untyped tool and report data
uses the combined exclusion list when a field's source is unknown.

### Verify from a local workstation

1. In Frappe Desk on a test site, open **Customize Form** for **Employee**.
   Add a temporary Small Text field labeled **Phase Three Private Note**.
   Use its actual fieldname in the YAML below. On the Employee record linked
   to your regular chat user, save the harmless test value
   `PHASE3-LOCAL-ONLY`.
2. In a local shell at the repository root, create and select a test pack.

   ```bash
   export KNOWLEDGE_PACK_DIR="$(mktemp -d)"
   export KNOWLEDGE_PACKS=hrms_core,erpnext_core,phase3_local
   cat >"$KNOWLEDGE_PACK_DIR/phase3_local.yaml" <<'YAML'
   pack_name: phase3_local
   never_send:
     doctype_fields:
       Employee: [custom_phase_three_private_note]
       Leave Application: [description]
   YAML
   ```

   For multi-site setups, append `phase3_local` to the selected site's
   `knowledge_packs` in `sites.yaml` and set `KNOWLEDGE_PACK_DIR` in both
   terminal sessions.

3. Start or restart the app locally with the usual chat credentials in
   `.env`. The startup log should list `phase3_local`.

   ```bash
   DEV_MODE=false FRAPPE_AUTH_MODE=user_session .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
   ```

4. At `http://localhost:8100/`, sign in as the regular user linked to the
   test Employee. Ask **Show my profile** and confirm ordinary profile
   details still appear. Then ask **Show my Phase Three Private Note**.
   Check the chat and the `/chat` response in your browser's Network tab
   for the absence of `PHASE3-LOCAL-ONLY`. If the user has a leave application,
   request a **leave history CSV** covering its date and confirm the Reason
   column is absent because that export uses `Leave Application.description`.
5. In the indexing terminal, set the same pack settings and use the
   dedicated indexing credentials from Phase 2. Keep the same embedding
   provider and index directory. Rebuild once, then inspect the Employee
   schema card.

   ```bash
   .venv/bin/python -m app.indexer sync_metadata
   .venv/bin/python -m app.indexer search schema_index Employee --limit 1
   ```

   Expect the Employee schema card to omit `custom_phase_three_private_note`.
   A snapshot built under older pack rules must be synced before it can be
   searched. Leave the indexing credentials in the indexing terminal.

Run the focused regression tests locally with:

```bash
.venv/bin/python -m pytest -q tests/test_knowledge_packs.py tests/test_metadata_index.py
```

## Phase 4 permission-scoped chat reads

The local chat backend loads a compatible index for its configured site and
embedding provider. Sync metadata first with your dedicated indexing account.
Start chat in a different terminal with the usual chat configuration from
`.env`. Use a regular user's own Frappe token in the browser. The universal
tools require that signed-in user's credentials, so the existing dev employee
picker is suitable for legacy HR tools only.

For the single site tested in Phase 2 and the pack tested in Phase 3, use:

```bash
cd ~/ai_backend
export EMBEDDING_PROVIDER=local
export METADATA_INDEX_DIR=metadata_indexes/local-test
export KNOWLEDGE_PACK_DIR="$PWD/metadata_indexes/local-test/phase3-packs"
export KNOWLEDGE_PACKS=hrms_core,erpnext_core,phase3_local

DEV_MODE=false FRAPPE_AUTH_MODE=user_session .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Use the same pack and embedding settings that built your snapshot. If your site
uses the default packs, omit the two pack exports. With a multi-site setup,
open the hostname for that site and use the embedding provider and active packs
from the site's YAML.

Open `http://localhost:8100/` and run these checks:

| Check | Example message | Pass condition |
| --- | --- | --- |
| Fast path | Hello | Chat replies without a record, report, or metadata lookup after authentication. |
| Record list | List the latest five submitted Sales Invoices with posting date and grand total. | The invoices and amounts match what the signed-in account can see in Frappe Desk. Use an equivalent populated DocType if Sales Invoice is unavailable on your site. |
| Follow-up | What about last month? | The same conversation keeps the invoice topic and switches to the previous calendar month. |
| Record details | Show Sales Invoice `<an ID visible to this user>`. | The response uses that exact record. Fields and child rows are limited to Frappe's permissions and the active exclusion rules. |
| Aggregation | Count Sales Invoices by docstatus for this month. | Each returned count matches the same filters in Frappe Desk. |
| Named report | Run Monthly Attendance Sheet for October 2026 for `<company>`. | The report executes under the signed-in user's permissions. Chat can ask for required filter values. Compare against the report with the same filters in Frappe Desk. |
| Access and redaction | Ask for a record this account cannot read, or ask for the Phase Three Private Note. | Chat gives no restricted record or excluded value. |
| Write routing | Cancel Sales Invoice `<ID>`. | Chat requires a Phase 5 confirmation before attempting a change. Use Frappe Desk to confirm the invoice status. |

Use your locally configured audit log to see which tool executed. The new tool
names are `search_schema`, `query_records`, `get_record_detail`,
`aggregate_records`, and `run_frappe_report`. The existing HR tools continue to
handle personal profile, payroll, policy, pictures, and downloadable HR
exports. For a missing or stale snapshot, reconcile the site, active packs and
embedding settings, then run `sync_metadata` in the indexing terminal.

The read path only exposes fields it can verify in a current response from
Frappe. On an empty list it can verify the DocType and keep the ID field in
the schema. Use a populated DocType for field and aggregation checks.

```bash
.venv/bin/python -m pytest -q tests/test_phase4_reads.py tests/test_metadata_index.py tests/test_knowledge_packs.py
```

## Phase 5 confirmed writes

Chat writes are available for explicitly enabled DocTypes. Set
`WRITE_DOCTYPES=ToDo` for a single-site local test. A multi-site setup uses
`write_doctypes: [ToDo]` under the site's entry. Add only the DocTypes you
intend to test. The chat user supplies their own Frappe credentials and
Frappe still checks native permissions. In Desk, the widget forwards the
current session's CSRF token with the confirmation request.

### Start the local backend

Use your usual chat configuration and a regular user token. Phase 5 proposals
use live Frappe metadata, so you can test `ToDo` independently of the Phase 4
invoice and report fixtures.

```bash
cd ~/ai_backend
WRITE_DOCTYPES=ToDo DEV_MODE=false FRAPPE_AUTH_MODE=user_session \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

### Test in chat and Frappe Desk

1. Sign into the local chat as a user who can create and edit their own ToDos.
   Ask **Create a ToDo with description "Phase 5 confirmation check".**
   Chat should display a **Create ToDo** card showing the exact field. Search
   for the description in Desk before confirming and verify it is absent.
   You can type **reviewed** in the same chat to show the card again. The
   card's **Confirm create** button applies the change.
2. Click **Discard**. Refresh the Desk list and verify the test ToDo is still
   absent. Ask for it again and click **Confirm create**. Copy the returned
   record ID and open that record in Desk.
3. Ask **Update ToDo <record ID> and change description to "Phase 5 updated".**
   Verify the card shows the original and new descriptions. Click
   **Confirm update** and compare the saved field in Desk.
4. Ask for a second update, then edit the same ToDo directly in Desk before
   clicking **Confirm update**. The backend should reject the older proposal.
   Refresh Desk and verify the newer change is present.
5. Ask **Delete ToDo <record ID>** for the test record. Confirm the deletion
   only once you have checked the exact ID on the card. Refresh Desk and
   verify the test record is gone.

ToDo supports create, update, and delete. To test submission and cancellation,
enable a submittable test DocType as well. Use a draft for **Submit** and a
submitted record for **Cancel**. The same user needs the corresponding Frappe
permissions and Write access. Verify each proposed state in Desk before and
after confirmation. Chat updates apply to draft records and support scalar
fields. Creates support scalar fields and a bounded set of child rows.

Confirmations expire after five minutes and are single-use. Run proposal and
confirmation against the same backend process. A restart clears outstanding
cards. An existing record is checked again at confirmation, and updates,
submission and cancellation pass the reviewed version to Frappe for its
timestamp check. Frappe's native DELETE endpoint has no version argument, so
the backend checks the record immediately before calling it.

```bash
.venv/bin/python -m pytest -q tests/test_mutations.py
```
