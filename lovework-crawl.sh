#!/usr/bin/env bash
# LoveWork crawl wrapper — locking + logging + notification
#
# Usage: lovework-crawl.sh <full|incremental>
#   full        — all sources, full report
#   incremental — neolabs + gmail + HN hiring + HN jobs
#
# Lock file at cache/crawl.lock prevents concurrent runs.
# Log file at logs/<type>-<timestamp>.log — dashboard picks it up live.
# On success: outputs summary, emails LJ
# On failure: outputs error (Hermes cron delivers via Telegram)

set -euo pipefail

CRAWL_TYPE="${1:?Usage: lovework-crawl.sh <full|incremental>}"
LOVEWORK_ROOT="$HOME/LJ-work-2026/lovework"
AGENT_DIR="$LOVEWORK_ROOT/lovework-agent"
VENV_PYTHON="$LOVEWORK_ROOT/venv/bin/python3"
LOCK_FILE="$AGENT_DIR/cache/crawl.lock"
LOG_DIR="$AGENT_DIR/logs"
MAIL_TO="LjubomirJosifovski@gmail.com"
DATE_STR=$(date +"%Y-%m-%d %H:%M")
TS=$(date +"%Y%m%d-%H%M%S")
LOG_FILE="$LOG_DIR/$CRAWL_TYPE-$TS.log"

# LoveWork always runs inside a Hermes profile.  Hermes may provide
# HERMES_HOME explicitly; otherwise use the homelab host mapping.  Unknown
# hosts require LOVEWORK_HERMES_PROFILE to be set by the user.
HOST_SHORT=$(hostname -s | tr '[:upper:]' '[:lower:]')
HERMES_BASE="${LOVEWORK_HERMES_BASE:-$HOME/.hermes-$HOST_SHORT}"
if [ -z "${HERMES_HOME:-}" ]; then
    case "$HOST_SHORT" in
        gigul2) HERMES_PROFILE="${LOVEWORK_HERMES_PROFILE:-hermel}" ;;
        macbook2) HERMES_PROFILE="${LOVEWORK_HERMES_PROFILE:-hermeo}" ;;
        *) HERMES_PROFILE="${LOVEWORK_HERMES_PROFILE:-}" ;;
    esac
    if [ -z "$HERMES_PROFILE" ]; then
        echo "No Hermes profile configured for host $HOST_SHORT; set LOVEWORK_HERMES_PROFILE" >&2
        exit 78
    fi
    HERMES_HOME="$HERMES_BASE/profiles/$HERMES_PROFILE"
fi
export HERMES_HOME
HERMES_PROFILE_NAME=$(basename "$HERMES_HOME")

mkdir -p "$LOG_DIR"

# --------------- Lock ---------------
acquire_lock() {
    if [ -f "$LOCK_FILE" ]; then
        local OLD_PID
        OLD_PID=$(sed -n 's/^pid=//p' "$LOCK_FILE" | head -1)
        if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
            echo "[LOCK] Crawl already running (PID $OLD_PID). Exiting."
            exit 75  # EX_TEMPFAIL — cron will retry
        fi
        echo "[LOCK] Stale lock from PID $OLD_PID — removing."
        rm -f "$LOCK_FILE"
    fi
    {
        echo "pid=$$"
        echo "type=$CRAWL_TYPE"
        echo "start=$(date -Iseconds)"
        echo "start_epoch=$(date +%s)"
        echo "status=running"
    } > "$LOCK_FILE"
    trap release_lock EXIT
}

release_lock() {
    # Mark finished in lock before removing (in case dashboard reads it mid-remove)
    { echo "status=finished"; } >> "$LOCK_FILE" 2>/dev/null || true
    rm -f "$LOCK_FILE"
}

# --------------- Notification helpers ---------------
send_email() {
    local SUBJECT="$1"
    local BODY="$2"
    local GAPI_SCRIPT="$HERMES_HOME/skills/productivity/google-workspace/scripts/google_api.py"
    local VENV_PYTHON="$LOVEWORK_ROOT/venv/bin/python3"
    if [ -f "$GAPI_SCRIPT" ] && [ -f "$HERMES_HOME/google_token.json" ]; then
        echo "$BODY" | "$VENV_PYTHON" "$GAPI_SCRIPT" gmail send \
            --to "$MAIL_TO" --subject "$SUBJECT" 2>/dev/null && \
            echo "[EMAIL] Sent via Gmail API" && return
    fi
    # Fallback: local mail (may bounce if no SMTP relay)
    echo "$BODY" | /usr/bin/mail -s "$SUBJECT" "$MAIL_TO" 2>/dev/null && \
        echo "[EMAIL] Sent via local mail" || \
        echo "[EMAIL] Could not send — Gmail API token may need refresh. Run:"
    echo "  $GAPI_SCRIPT oauth"
}

# --------------- Main ---------------
acquire_lock

echo "LoveWork $CRAWL_TYPE sweep — $DATE_STR"
echo "Hermes profile: $HERMES_PROFILE_NAME ($HERMES_HOME)"
echo "Log: $LOG_FILE"
echo "Running..."

cd "$AGENT_DIR"

if [ "$CRAWL_TYPE" = "full" ]; then
    $VENV_PYTHON main.py --profile lj --role general --source all --report \
        2>&1 | tee -a "$LOG_FILE"
elif [ "$CRAWL_TYPE" = "incremental" ]; then
    $VENV_PYTHON incremental_crawl.py \
        2>&1 | tee -a "$LOG_FILE"
else
    echo "Unknown crawl type: $CRAWL_TYPE"
    exit 1
fi

CRAWL_EXIT=${PIPESTATUS[0]}

if [ $CRAWL_EXIT -ne 0 ]; then
    echo "[CRAWL FAILED] exit code $CRAWL_EXIT"
    echo "[CRAWL FAILED] exit code $CRAWL_EXIT" >> "$LOG_FILE"
    exit $CRAWL_EXIT
fi

echo "[CRAWL COMPLETE]" >> "$LOG_FILE"

# Regenerate the manual so dashboard picks up latest stats
set +e
$VENV_PYTHON build_manual.py >> "$LOG_FILE" 2>&1
set -e

# Success — send email summary
LATEST_REPORT=$(ls -t wiki/reports/*.md 2>/dev/null | head -1)
if [ -n "$LATEST_REPORT" ]; then
    SUMMARY=$(head -60 "$LATEST_REPORT" | grep -E "^###|^- |^\*\*|^#|Score|GO|MAYBE" | head -30)
EMAIL_BODY="LoveWork $CRAWL_TYPE sweep completed at $DATE_STR.
Hermes profile: $HERMES_PROFILE_NAME ($HERMES_HOME)

Top findings:
$SUMMARY

Dashboard: http://192.168.1.251:8765/
Full report: $LATEST_REPORT"
else
    EMAIL_BODY="LoveWork $CRAWL_TYPE sweep completed at $DATE_STR.
No report file found."
fi

send_email "LoveWork — $CRAWL_TYPE sweep done ($DATE_STR)" "$EMAIL_BODY"
echo "[EMAIL] Summary sent to $MAIL_TO"
