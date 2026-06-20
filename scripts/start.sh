#!/usr/bin/env bash
# ============================================================
#  Shield Bot – Production Entry Point
#  Order: git-lock-clear → github-push → deps → db-init → preflight → bot (auto-restart)
#
#  GitHub strategy: We PUSH our production code to shield-bot-v2.
#  We do NOT pull from it (that branch has a different/incomplete structure).
# ============================================================

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOGS_DIR="$ROOT/logs"
DATA_DIR="$ROOT/data"
APP_LOG="$LOGS_DIR/app.log"
ERR_LOG="$LOGS_DIR/error.log"
MAX_RESTARTS=20
RESTART_DELAY=5

mkdir -p "$LOGS_DIR" "$DATA_DIR"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*" | tee -a "$APP_LOG"; }
warn() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN]  $*" | tee -a "$APP_LOG"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" | tee -a "$APP_LOG" "$ERR_LOG" >&2; }

log "========================================"
log " Shield Bot – Production Start"
log " $(date '+%Y-%m-%d %H:%M:%S')"
log "========================================"

# ── 0. Clear stale git index lock (Python, non-destructive) ─
python3 - 2>/dev/null <<'PYEOF'
import pathlib
lock = pathlib.Path(".git/index.lock")
if lock.exists():
    lock.unlink()
    print("Git: cleared stale index.lock")
PYEOF

# ── 1. Git identity ─────────────────────────────────────────
git config user.email "shield-bot@replit.local" 2>/dev/null || true
git config user.name  "Shield Bot CI"            2>/dev/null || true

# ── 2. GitHub Push (sync our production code to shield-bot-v2) ─
TARGET_BRANCH="shield-bot-v2"

if [ -n "${GITHUB_TOKEN:-}" ]; then
    REPO_URL="https://${GITHUB_TOKEN}@github.com/youneszas1995-cmd/Shield---.git"
    git remote set-url origin "$REPO_URL" 2>>"$ERR_LOG" || true

    log "GitHub: staging production files for push to '$TARGET_BRANCH'…"

    # Stage only source-controlled production files (never secrets/sessions/logs/data)
    git add \
        bot.py \
        db_init.py \
        requirements.txt \
        scripts/start.sh \
        .gitignore \
        2>>"$ERR_LOG" || true

    if ! git diff --cached --quiet 2>/dev/null; then
        COMMIT_MSG="chore: Replit production sync [$(date '+%Y-%m-%d %H:%M')]"
        if git commit -m "$COMMIT_MSG" 2>>"$ERR_LOG"; then
            if git push origin "HEAD:$TARGET_BRANCH" --force 2>>"$ERR_LOG"; then
                log "GitHub push: ✅ changes pushed to $TARGET_BRANCH"
            else
                warn "GitHub push: push failed (permissions or network) – non-fatal"
            fi
        else
            warn "GitHub push: commit failed – non-fatal"
        fi
    else
        log "GitHub push: nothing new to push"
    fi

    # Restore URL without token
    git remote set-url origin "https://github.com/youneszas1995-cmd/Shield---.git" 2>/dev/null || true
else
    warn "GitHub sync: GITHUB_TOKEN not set – running in offline mode"
fi

# ── 2b. Railway environment check ───────────────────────────
# Railway automatically sets RAILWAY_ENVIRONMENT (e.g. "production").
# If it is NOT set we are on Replit or a local machine — do git sync only,
# then exit cleanly to prevent a duplicate Telegram session alongside Railway.
if [ -z "${RAILWAY_ENVIRONMENT:-}" ]; then
    log "⚠️  RAILWAY_ENVIRONMENT not detected — running in Replit/local mode."
    log "    Git sync complete. Bot startup skipped: Railway is the primary runtime."
    log "    To run the bot here, set RAILWAY_ENVIRONMENT=production in Replit secrets."
    exit 0
fi

# ── 3. Install / verify dependencies ───────────────────────
log "Installing dependencies…"
pip install -q -r requirements.txt 2>>"$ERR_LOG" \
    && log "Dependencies: ✅ OK" \
    || { err "Dependency install failed – aborting"; exit 1; }

# Guard: remove conflicting 'decouple' package if installed
if python3 -c "import decouple; import sys; sys.exit(0 if not hasattr(decouple,'Config') else 1)" 2>/dev/null; then
    pip uninstall decouple -y -q 2>>"$ERR_LOG" || true
    warn "Removed conflicting 'decouple' package"
fi

# ── 4. Database initialisation ─────────────────────────────
log "Initialising database…"
python3 db_init.py 2>>"$ERR_LOG" \
    && log "Database: ✅ data/shield.db ready" \
    || { err "Database init failed – aborting"; exit 1; }

# ── 5. Pre-flight import check ─────────────────────────────
log "Pre-flight import check…"
python3 - 2>>"$ERR_LOG" <<'PYEOF'
from telethon import TelegramClient, events, Button
from decouple import config
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
print("[OK] All imports verified")
PYEOF
[ $? -eq 0 ] && log "Pre-flight: ✅ all imports OK" || { err "Import check failed – aborting"; exit 1; }

log "========================================"
log " System ready – starting bot…"
log "========================================"

# ── 6. Bot runner with auto-recovery ───────────────────────
RESTART_COUNT=0

while true; do
    log "Bot starting (run #${RESTART_COUNT})…"
    python3 -u bot.py 2>&1 | tee -a "$APP_LOG"
    EXIT_CODE=${PIPESTATUS[0]}
    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ "$EXIT_CODE" -eq 0 ]; then
        log "Bot exited cleanly – restarting…"
    else
        err "Bot crashed (exit=$EXIT_CODE) – restart #${RESTART_COUNT} in ${RESTART_DELAY}s"
    fi

    if [ "$RESTART_COUNT" -ge "$MAX_RESTARTS" ]; then
        warn "Hit $MAX_RESTARTS restarts – cooling down 60s then resetting counter"
        sleep 60
        RESTART_COUNT=0
    fi

    sleep "$RESTART_DELAY"
done
