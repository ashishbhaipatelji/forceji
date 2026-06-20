#!/usr/bin/env bash
# ============================================================
#  Shield Bot – Production Entry Point
#  Order: git-unlock → github-pull → deps → db-init → preflight → bot (auto-restart)
#  GitHub push: local production changes are committed & pushed on startup
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

# ── 0. Clear stale git lock (safe, non-destructive) ────────
python3 - <<'PYEOF' 2>/dev/null && log "Git: index.lock cleared" || true
import os, pathlib
lock = pathlib.Path(".git/index.lock")
if lock.exists():
    lock.unlink()
    print("removed index.lock")
PYEOF

# ── 1. Git identity ─────────────────────────────────────────
git config user.email "shield-bot@replit.local" 2>/dev/null || true
git config user.name  "Shield Bot CI"            2>/dev/null || true

# ── 2. GitHub Pull ─────────────────────────────────────────
TARGET_BRANCH="shield-bot-v2"
GIT_OK=false

if [ -n "${GITHUB_TOKEN:-}" ]; then
    REPO_URL="https://${GITHUB_TOKEN}@github.com/youneszas1995-cmd/Shield---.git"
    git remote set-url origin "$REPO_URL" 2>>"$ERR_LOG" || true

    log "GitHub pull: fetching latest from '$TARGET_BRANCH'…"
    if git fetch origin "$TARGET_BRANCH" 2>>"$ERR_LOG"; then
        # Safely pull bot.py from remote (never overwrite our infra files)
        if git checkout "origin/$TARGET_BRANCH" -- bot.py 2>>"$ERR_LOG"; then
            log "GitHub pull: ✅ bot.py synced from $TARGET_BRANCH"
        else
            warn "GitHub pull: bot.py unchanged or already up to date"
        fi
        GIT_OK=true
    else
        err "GitHub pull: fetch failed (git lock or network) – using local code"
    fi

    # ── 3. GitHub Push (commit & push production infra changes) ──
    if [ "$GIT_OK" = true ]; then
        # Only track production source files, never secrets/sessions/logs
        git add \
            bot.py \
            db_init.py \
            requirements.txt \
            scripts/start.sh \
            .gitignore \
            2>>"$ERR_LOG" || true

        if ! git diff --cached --quiet 2>/dev/null; then
            COMMIT_MSG="chore: auto-sync from Replit [$(date '+%Y-%m-%d %H:%M')]"
            git commit -m "$COMMIT_MSG" 2>>"$ERR_LOG" \
                && git push origin "HEAD:$TARGET_BRANCH" 2>>"$ERR_LOG" \
                && log "GitHub push: ✅ local changes pushed to $TARGET_BRANCH" \
                || warn "GitHub push: push failed (non-fatal)"
        else
            log "GitHub push: nothing new to push"
        fi
    fi

    # Restore URL without token for safety
    git remote set-url origin "https://github.com/youneszas1995-cmd/Shield---.git" 2>/dev/null || true
else
    warn "GitHub sync: GITHUB_TOKEN not set – running in offline mode"
fi

# ── 4. Install / verify dependencies ───────────────────────
log "Installing dependencies…"
if pip install -q -r requirements.txt 2>>"$ERR_LOG"; then
    log "Dependencies: ✅ OK"
else
    err "Dependency install failed – aborting"
    exit 1
fi

# Guard: remove conflicting 'decouple' package if present
python3 -c "import decouple; print(decouple.__version__)" 2>/dev/null \
    | grep -q "." \
    && { pip uninstall decouple -y -q 2>>"$ERR_LOG"; warn "Removed conflicting 'decouple' package"; } \
    || true

# ── 5. Database initialisation ─────────────────────────────
log "Initialising database…"
if python3 db_init.py 2>>"$ERR_LOG"; then
    log "Database: ✅ data/shield.db ready"
else
    err "Database init failed – aborting"
    exit 1
fi

# ── 6. Pre-flight import check ─────────────────────────────
log "Pre-flight import check…"
python3 - 2>>"$ERR_LOG" <<'PYEOF'
from telethon import TelegramClient, events, Button
from decouple import config
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
print("[OK] All imports verified")
PYEOF
if [ $? -ne 0 ]; then
    err "Import check failed – aborting"
    exit 1
fi
log "Pre-flight: ✅ all imports OK"

log "========================================"
log " System ready. Starting bot…"
log "========================================"

# ── 7. Bot runner with auto-recovery ───────────────────────
RESTART_COUNT=0

while true; do
    log "Bot starting (run #${RESTART_COUNT})…"
    python3 -u bot.py 2>&1 | tee -a "$APP_LOG"
    EXIT_CODE=${PIPESTATUS[0]}
    RESTART_COUNT=$((RESTART_COUNT + 1))

    if [ "$EXIT_CODE" -eq 0 ]; then
        log "Bot exited cleanly."
    else
        err "Bot crashed (exit=$EXIT_CODE) – restart #${RESTART_COUNT} in ${RESTART_DELAY}s"
    fi

    if [ "$RESTART_COUNT" -ge "$MAX_RESTARTS" ]; then
        warn "Hit max restarts ($MAX_RESTARTS) – cooling down 60s then resetting counter"
        sleep 60
        RESTART_COUNT=0
    fi

    sleep "$RESTART_DELAY"
done
