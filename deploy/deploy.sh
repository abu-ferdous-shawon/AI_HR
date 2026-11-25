
#!/usr/bin/env bash
set -Eeuo pipefail

# ==========================================
# AI_HR Automated Deployment
# ==========================================

APP_DIR="$HOME/projects/AI_HR"
BRANCH="main"
SERVICE="hr-ai-chatbot.service"
HEALTH_URL="http://127.0.0.1:8101/openapi.json"
BACKUP_DIR="$HOME/.local/share/ai-hr-deploy/backups"
LOCK_FILE="/tmp/ai-hr-deploy-$(id -u).lock"

log() {
    echo
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

# Prevent simultaneous deployments
exec 9>"$LOCK_FILE"
flock -n 9 || fail "Another deployment is running."

cd "$APP_DIR" || fail "Project directory not found."

log "Checking prerequisites..."

for cmd in git curl sudo systemctl flock; do
    command -v "$cmd" >/dev/null ||
        fail "Missing command: $cmd"
done

[[ -x .venv/bin/python ]] ||
    fail "Python virtual environment not found."

[[ -f requirements.txt ]] ||
    fail "requirements.txt not found."

[[ -f .env ]] ||
    fail ".env file not found."

# Check Git status
log "Checking Git working tree..."

[[ "$(git branch --show-current)" == "$BRANCH" ]] ||
    fail "Switch to the $BRANCH branch first."

[[ -z "$(git status --porcelain --untracked-files=no)" ]] ||
    fail "Tracked files have uncommitted changes. Commit or stash them first."

# Ensure environment configuration cannot be overwritten by Git
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    fail ".env is tracked by Git. Move secrets out of version control first."
fi

# Ensure script can be read after pull
log "Backing up environment configuration..."

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

BACKUP_FILE="$BACKUP_DIR/env-$(date +%Y%m%d-%H%M%S)-$$.backup"

cp -p .env "$BACKUP_FILE"
chmod 600 "$BACKUP_FILE"

log "Environment backup: $BACKUP_FILE"

# Remember current revision
OLD_COMMIT="$(git rev-parse --short HEAD)"

# Fetch and update
log "Fetching latest code..."

git fetch origin "$BRANCH"

log "Updating application..."

git merge --ff-only "origin/$BRANCH"

NEW_COMMIT="$(git rev-parse --short HEAD)"

log "Previous commit: $OLD_COMMIT"
log "Current commit:  $NEW_COMMIT"

# Install Python dependencies
log "Installing Python dependencies..."

.venv/bin/python -m pip install -r requirements.txt

log "Checking dependency consistency..."

.venv/bin/python -m pip check

# Validate Python source syntax
log "Checking application Python syntax..."

.venv/bin/python - <<'PY'
from pathlib import Path
import ast

count = 0

for path in Path("app").rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count += 1

print(f"Validated {count} Python files.")
PY

# Restart only the AI chatbot service
log "Restarting $SERVICE..."

sudo systemctl restart "$SERVICE"

log "Checking systemd service..."

sudo systemctl is-active --quiet "$SERVICE" ||
    fail "Service failed to start. Check journalctl logs."

# Check API readiness
log "Checking API health..."

HEALTH_OK=false

for attempt in {1..15}; do
    HTTP_CODE="$(curl --silent --show-error \
        --max-time 5 \
        --output /dev/null \
        --write-out '%{http_code}' \
        "$HEALTH_URL" 2>/dev/null)" || HTTP_CODE="000"

    if [[ "$HTTP_CODE" == "200" ]]; then
        HEALTH_OK=true
        break
    fi

    echo "Health check $attempt/15: HTTP $HTTP_CODE"
    sleep 2
done

if [[ "$HEALTH_OK" != true ]]; then
    echo
    sudo journalctl -u "$SERVICE" -n 40 --no-pager || true
    fail "API health check failed."
fi

# Final verification
log "Checking service stability..."

sudo systemctl is-active --quiet "$SERVICE" ||
    fail "Service stopped after the health check."

echo
echo "=========================================="
echo "       AI_HR DEPLOYMENT SUCCESSFUL"
echo "=========================================="
echo "Previous commit : $OLD_COMMIT"
echo "Current commit  : $NEW_COMMIT"
echo "Service         : $SERVICE"
echo "API             : HTTP 200"
echo "Environment     : Preserved"
echo "=========================================="

git log -1 --oneline
