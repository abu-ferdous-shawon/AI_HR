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
`app/knowledge_packs/hrms_core.yaml` for the Phase 2 alias, vocabulary and
field exclusion format. Use `index_include_doctypes` for specific built-in
exclusions you want to include. Use `index_exclude_doctypes` for site-specific
exclusions. The environment equivalents accept comma-separated names.

The chat UI still uses the existing tools. Phase 4 connects the indexes to
chat retrieval.
