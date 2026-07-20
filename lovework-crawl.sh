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
DATE_STR=$(date +"%Y-%m-%d %H:%M")
TS=$(date +"%Y%m%d-%H%M%S")
LOG_FILE="$LOG_DIR/$CRAWL_TYPE-$TS.log"
RUN_ID="$CRAWL_TYPE-$TS-$$"
RUN_LEDGER="$AGENT_DIR/run_ledger.py"
NOTIFIER="$AGENT_DIR/notify.py"

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

log() {
    printf '%s\n' "$*" | tee -a "$LOG_FILE"
}

record_start() {
    "$VENV_PYTHON" "$RUN_LEDGER" start \
        --run-id "$RUN_ID" --run-type "$CRAWL_TYPE" \
        --profile "$HERMES_PROFILE_NAME" --hermes-home "$HERMES_HOME" \
        --log-file "$LOG_FILE" --pid "$$" >> "$LOG_FILE" 2>&1
}

record_finish() {
    local STATUS="$1"
    local EXIT_CODE="$2"
    local REPORT_FILE="${3:-}"
    local ERROR_TEXT="${4:-}"
    local ARGS=(finish --run-id "$RUN_ID" --status "$STATUS" --exit-code "$EXIT_CODE")
    if [ -n "$REPORT_FILE" ]; then
        ARGS+=(--report-file "$REPORT_FILE")
    fi
    if [ -n "$ERROR_TEXT" ]; then
        ARGS+=(--error "$ERROR_TEXT")
    fi
    "$VENV_PYTHON" "$RUN_LEDGER" "${ARGS[@]}" >> "$LOG_FILE" 2>&1 || true
}

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

# --------------- Main ---------------
acquire_lock
record_start

FINALIZED=0
finish_unexpectedly() {
    local EXIT_CODE=$?
    if [ "$FINALIZED" -eq 0 ]; then
        record_finish "failed" "$EXIT_CODE" "" "wrapper exited before terminal run resolution"
    fi
    release_lock
    trap - EXIT
    exit "$EXIT_CODE"
}
trap finish_unexpectedly EXIT

log "LoveWork $CRAWL_TYPE sweep — $DATE_STR"
log "Run ID: $RUN_ID"
log "Hermes profile: $HERMES_PROFILE_NAME ($HERMES_HOME)"
log "Log: $LOG_FILE"
log "Running..."

cd "$AGENT_DIR"

if [ "$CRAWL_TYPE" = "full" ]; then
    set +e
    "$VENV_PYTHON" main.py --profile lj --role general --source all --report \
        2>&1 | tee -a "$LOG_FILE"
    CRAWL_EXIT=${PIPESTATUS[0]}
    set -e
elif [ "$CRAWL_TYPE" = "incremental" ]; then
    set +e
    "$VENV_PYTHON" incremental_crawl.py \
        2>&1 | tee -a "$LOG_FILE"
    CRAWL_EXIT=${PIPESTATUS[0]}
    set -e
else
    log "Unknown crawl type: $CRAWL_TYPE"
    exit 1
fi

if [ $CRAWL_EXIT -ne 0 ]; then
    log "[CRAWL FAILED] exit code $CRAWL_EXIT"
    record_finish "failed" "$CRAWL_EXIT" "" "crawl process exited $CRAWL_EXIT"
    FINALIZED=1
    exit $CRAWL_EXIT
fi

log "[CRAWL COMPLETE]"

# Regenerate the manual so dashboard picks up latest stats
if ! "$VENV_PYTHON" build_manual.py >> "$LOG_FILE" 2>&1; then
    log "[MANUAL FAILED] build_manual.py"
    record_finish "failed" 1 "" "build_manual.py failed after successful crawl"
    FINALIZED=1
    exit 1
fi

# A crawl is only a successful operational run when it yields its report.
LATEST_REPORT=$(ls -t wiki/reports/*.md 2>/dev/null | head -1)
if [ -z "$LATEST_REPORT" ]; then
    log "[REPORT FAILED] No report file found after successful crawl"
    record_finish "failed" 1 "" "no report file found after successful crawl"
    FINALIZED=1
    exit 1
fi

record_finish "succeeded" 0 "$LATEST_REPORT"
if "$VENV_PYTHON" "$NOTIFIER" --report "$LATEST_REPORT" --log "$LOG_FILE" --run-id "$RUN_ID" >> "$LOG_FILE" 2>&1; then
    log "[EMAIL] Gmail API delivery evidenced"
else
    log "[EMAIL FAILED] Crawl succeeded; watchdog must reconcile notification failure"
fi

FINALIZED=1
