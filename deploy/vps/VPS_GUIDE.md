# AI_HR deployment and updates — ecosystem VPS

This guide records the deployment verified on 18 November 2026. Run commands on
the VPS as `ecosystem`. The current deployment is complete: use **section 5 for
future updates**, not the initial setup again.

## 1. Deployment layout

| Component | Path / address |
|---|---|
| User-facing Frappe + chatbot | `http://192.168.10.240:8001/desk` |
| Alternate testing URL | `http://192.168.10.240:8085/desk` |
| Repository | `/home/ecosystem/projects/AI_HR` |
| Bench / site | `/home/ecosystem/projects/version-16` / `internalhrms.skl` |
| Frappe HTTP / Socket.IO | `18001` / `9001` |
| Chatbot HTTP | `127.0.0.1:8101` |
| Services | `frappe-v16.service`, `hr-ai-chatbot.service`, `nginx.service` |
| Nginx configuration | `/etc/nginx/conf.d/hr-ai-test.conf` |

Nginx owns ports **8001 and 8085**. It forwards `/ai/` to the chatbot,
`/socket.io/` to 9001, and everything else to Frappe on 18001.
The old manual chatbot on 8100 is unnecessary; stop its terminal with Ctrl+C.

Do not change `frappe.service`, `first-one-travels.service`, or regenerate the
whole server's Nginx configuration. A shared Nginx reload applies any pending
configuration changes for other sites too.

## 2. Initial backend and widget setup (already done)

Prerequisites: existing working v16 bench, Nginx, Git, Python with `venv`, sudo,
and outbound access to the configured LLM. Before rebuilding this deployment,
check that 18001 and 8101 are free and save copies of the bench's `Procfile`,
`sites/common_site_config.json`, and any Nginx file you will edit.

```bash
cd ~/projects
git clone git@github.com:abu-ferdous-shawon/AI_HR.git
cd AI_HR
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p logs policies
nano .env
```

Set these values; obtain API credentials from a dedicated Frappe integration
user with the required HR read permissions, and the Gemini key from your account:

```dotenv
FRAPPE_URL=http://127.0.0.1:18001
FRAPPE_API_KEY=REPLACE_ME
FRAPPE_API_SECRET=REPLACE_ME
FRAPPE_AUTH_MODE=user_session
REGISTRY_PROFILE=frappe_hrms_v16
DEV_MODE=false
ENVIRONMENT=production
TRUST_PROXY=true
LLM_PROVIDER=gemini
GEMINI_API_KEY=REPLACE_ME
GEMINI_MODEL=gemini-2.5-flash
POLICY_DIR=policies
POLICY_LANGUAGE=English
LOG_FILE=logs/app.log
AUDIT_LOG_FILE=logs/audit.jsonl
```

Use a model available to your provider account. Keep `.env` private
(`chmod 600 .env`), and put policy documents in `policies/`.
Each test employee needs a Frappe login linked through **Employee → User ID**.

Install the nested widget using a symlink, not `bench get-app <nested-folder>`:

```bash
cd ~/projects/version-16
ln -s /home/ecosystem/projects/AI_HR/frappe_app/hr_ai_widget apps/hr_ai_widget
./env/bin/python -m pip install -e apps/hr_ai_widget
./env/bin/python - <<'PY'
from pathlib import Path
p = Path("sites/apps.txt")
text = p.read_text().replace("erpnexthr_ai_widget", "erpnext\nhr_ai_widget")
apps = list(dict.fromkeys(x.strip() for x in text.splitlines() if x.strip()))
if "hr_ai_widget" not in apps:
    apps.append("hr_ai_widget")
p.write_text("\n".join(apps) + "\n")
PY
bench --site internalhrms.skl install-app hr_ai_widget
bench build --app hr_ai_widget
bench --site internalhrms.skl clear-cache
```

Skip `ln -s` if the correct link already exists. The widget package's
`hr_ai_widget/__init__.py` must define `__version__` (currently `"0.0.1"`).
Do not use `bench setup assets`; this deployment uses `bench build`.

## 3. Frappe ports and Nginx (already done)

In `~/projects/version-16/Procfile`, the web entry must be:

```text
web: bench serve --port 18001
```

In `sites/common_site_config.json`, set `"webserver_port": 18001`, preserving
all other keys. Keep `socketio_port` at **9001**. Restart only this bench to
release 8001 before Nginx begins listening there:

```bash
sudo systemctl restart frappe-v16.service
```

Install the supplied [Nginx configuration](vps/hr-ai-test.conf). It keeps both
8001 and 8085 working, including Socket.IO. Its trailing `/` on the chatbot
`proxy_pass` strips the `/ai/` prefix.

```bash
sudo cp ~/projects/AI_HR/deploy/vps/hr-ai-test.conf /etc/nginx/conf.d/hr-ai-test.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 4. Automatic startup (already configured)

Install the supplied [systemd unit](vps/hr-ai-chatbot.service):

```bash
sudo cp ~/projects/AI_HR/deploy/vps/hr-ai-chatbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable frappe-v16.service nginx.service hr-ai-chatbot.service
sudo systemctl restart hr-ai-chatbot.service
sudo systemctl status hr-ai-chatbot.service --no-pager
```

It runs as `ecosystem` on loopback port 8101 and restarts after failures.
The application reads `.env` from its working directory; shell activation is not
required. No manual startup is needed after a VPS reboot. A normal backend
update needs only a chatbot restart, not a reboot or Nginx reload. Editing the
unit requires `daemon-reload` followed by a restart.

## 5. Future updates from Git

First run `cd ~/projects/AI_HR && git status --short`. Resolve tracked local
changes before pulling; do not discard them blindly. In particular, the original
installation may have locally added `__version__` to the widget package.
Read the incoming release instructions for new `.env` keys or migrations.

Run this block during a short maintenance window. It stops on errors. If it
fails, inspect the error before continuing; the chatbot may remain stopped.

```bash
(
set -e
cd ~/projects/AI_HR
git diff --quiet
git diff --cached --quiet
git fetch

UPDATE_BACKUP=$(mktemp -d "$HOME/ai-hr-update.XXXXXX")
chmod 700 "$UPDATE_BACKUP"
git rev-parse HEAD > "$UPDATE_BACKUP/revision"
git symbolic-ref --short HEAD > "$UPDATE_BACKUP/branch"
cp -p .env "$UPDATE_BACKUP/env"
.venv/bin/python -m pip freeze > "$UPDATE_BACKUP/requirements.txt"
echo "Update backup: $UPDATE_BACKUP"

sudo systemctl stop hr-ai-chatbot.service
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check

if ! git diff --quiet "$(cat "$UPDATE_BACKUP/revision")" HEAD -- frappe_app/; then
    cd ~/projects/version-16
    ./env/bin/python -m pip install -e apps/hr_ai_widget
    bench build --app hr_ai_widget
    bench --site internalhrms.skl clear-cache
    sudo systemctl restart frappe-v16.service
fi

sudo systemctl start hr-ai-chatbot.service
sudo systemctl status hr-ai-chatbot.service --no-pager
)
```

`--ff-only` refuses an automatic merge. `.env`, logs, and policies are local
configuration/data; preserve them. Apply any required new `.env` settings before
starting the updated service. If `.env` changes later, restart the chatbot.
Backend-served UI changes need only the chatbot restart and browser hard refresh;
widget app changes also trigger the build/cache/bench restart above.

## 6. Verify, troubleshoot, and recover

```bash
for path in /api/method/ping /ai/health /ai/ai_chat.js /assets/hr_ai_widget/js/hr_ai_loader.js; do
    curl --fail --silent --show-error --retry 10 --retry-connrefused \
        --retry-delay 2 --max-time 10 -o /dev/null \
        -w "$path: %{http_code}\n" "http://127.0.0.1:8001$path"
done
sudo journalctl -u hr-ai-chatbot.service -n 80 --no-pager
```

Expect four **200** responses. Open the Desk URL, hard-refresh, and test a real
employee question. HTTP health alone does not verify login, HR permissions, or
LLM access. Startup retries can briefly print connection errors.

To undo a routine update, replace the backup path below with the one printed
by that update. This restores code, previous dependency versions, and `.env`;
it does not undo database migrations. Only run with a clean tracked working tree.

```bash
cd ~/projects/AI_HR
UPDATE_BACKUP=/home/ecosystem/ai-hr-update.REPLACE_ME
sudo systemctl stop hr-ai-chatbot.service
git switch --detach "$(cat "$UPDATE_BACKUP/revision")"
.venv/bin/python -m pip install -r "$UPDATE_BACKUP/requirements.txt"
cp -p "$UPDATE_BACKUP/env" .env
cd ~/projects/version-16
./env/bin/python -m pip install -e apps/hr_ai_widget
bench build --app hr_ai_widget
bench --site internalhrms.skl clear-cache
sudo systemctl restart frappe-v16.service
sudo systemctl start hr-ai-chatbot.service
```

The code is then detached at the previous revision. Before a later pull, switch
back to the branch recorded in the backup's `branch` file after the faulty
release is fixed. Re-run the verification checks.

The original port-migration backup is
`~/projects/AI_HR/deploy-backup.pxmuEg`; keep it private because it contains `.env`.
Restoring the original network layout requires coordinated Nginx and Frappe
changes: both cannot own port 8001 simultaneously.
