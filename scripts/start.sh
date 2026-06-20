#!/usr/bin/env bash
# ============================================================
#  Shield Bot – Production Entry Point
#  Run order: github-sync → deps → db-init → bot (auto-restart)
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
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" | tee -a "$APP_LOG" "$ERR_LOG" >&2; }

log "========================================"
log " Shield Bot – Production Start"
log "========================================"

# ── 1. GitHub Sync (bot.py only) ───────────────────────────
# We only pull bot.py from shield-bot-v2 so our production
# infrastructure files (this script, db_init.py, etc.) are
# never overwritten by the merge.
if [ -n "${GITHUB_TOKEN:-}" ]; then
    TARGET_BRANCH="shield-bot-v2"
    log "GitHub sync: fetching bot.py from '$TARGET_BRANCH'…"
    git config user.email "shield-bot@replit.local" 2>/dev/null || true
    git config user.name  "Shield Bot CI"            2>/dev/null || true
    REPO_URL="https://${GITHUB_TOKEN}@github.com/youneszas1995-cmd/Shield---.git"
    git remote set-url origin "$REPO_URL" 2>>"$ERR_LOG" || true
    if git fetch origin "$TARGET_BRANCH" 2>>"$ERR_LOG"; then
        git checkout "origin/$TARGET_BRANCH" -- bot.py 2>>"$ERR_LOG" \
            && log "GitHub sync: ✅ bot.py updated from $TARGET_BRANCH" \
            || log "GitHub sync: bot.py unchanged (no diff or checkout failed)"
    else
        err "GitHub sync: fetch failed – continuing with local code"
    fi
    git remote set-url origin "https://github.com/youneszas1995-cmd/Shield---.git" 2>/dev/null || true
else
    log "GitHub sync: GITHUB_TOKEN not set – skipping"
fi

# ── 2. Install / verify dependencies ───────────────────────
log "Installing dependencies…"
if pip install -q -r requirements.txt 2>>"$ERR_LOG"; then
    log "Dependencies: ✅ OK"
else
    err "Dependency install failed"
    exit 1
fi

# ── 3. Database initialisation ─────────────────────────────
log "Initialising database…"
if python3 db_init.py 2>>"$ERR_LOG"; then
    log "Database: ✅ OK"
else
    err "Database init failed"
    exit 1
fi

# ── 4. Pre-flight import check ─────────────────────────────
log "Running pre-flight import check…"
if python3 -c "
from telethon import TelegramClient, events, Button
from decouple import config
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
print('[OK] All imports verified')
" 2>>"$ERR_LOG"; then
    log "Pre-flight: ✅ all imports OK"
else
    err "Import check failed – aborting"
    exit 1
fi

# ── 5. Bot runner with auto-recovery ───────────────────────
RESTART_COUNT=0

while true; do
    log "Starting bot.py (run #${RESTART_COUNT})…"
    python3 -u bot.py 2>&1 | tee -a "$APP_LOG"
    EXIT_CODE=${PIPESTATUS[0]}

    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ "$EXIT_CODE" -eq 0 ]; then
        log "Bot exited cleanly."
    else
        err "Bot crashed (exit $EXIT_CODE). Restart #${RESTART_COUNT} in ${RESTART_DELAY}s…"
    fi

    if [ "$RESTART_COUNT" -ge "$MAX_RESTARTS" ]; then
        err "Reached $MAX_RESTARTS restarts. Cooling down 60s…"
        sleep 60
        RESTART_COUNT=0
    fi

    sleep "$RESTART_DELAY"
done
