#!/usr/bin/env bash
# Stop the SkillOpt Studio server started by start.sh.
# Eval/train jobs run in their own process sessions and are NOT killed —
# cancel them from the UI (or POST /api/jobs/<id>/cancel) before stopping
# if you don't want them to keep running.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${STUDIO_HOST:-127.0.0.1}"
PORT="${STUDIO_PORT:-8321}"
PID_FILE="outputs/studio/studio.pid"

# Warn about jobs that would be left running headless.
RUNNING_JOBS="$(curl -sf "http://$HOST:$PORT/api/jobs" 2>/dev/null \
    | python3 -c 'import json,sys; print(" ".join(j["id"] for j in json.load(sys.stdin) if j["status"] in ("running","queued")))' \
    2>/dev/null || true)"
if [[ -n "$RUNNING_JOBS" ]]; then
    echo "warning: active jobs will keep running after the server stops: $RUNNING_JOBS" >&2
fi

stopped=0

if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null && ps -p "$PID" -o args= | grep -q skillopt_studio; then
        PGID="$(ps -o pgid= -p "$PID" | tr -d ' ')"
        kill -TERM "-$PGID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "$PID" 2>/dev/null; then
            kill -KILL "-$PGID" 2>/dev/null || kill -KILL "$PID" 2>/dev/null || true
        fi
        stopped=1
    fi
    rm -f "$PID_FILE"
fi

# Fallback: a server started without start.sh (no/stale pidfile).
if pgrep -f "python3 -m skillopt_studio" >/dev/null 2>&1; then
    pkill -TERM -f "python3 -m skillopt_studio" || true
    sleep 1
    pkill -KILL -f "python3 -m skillopt_studio" 2>/dev/null || true
    stopped=1
fi

if [[ "$stopped" == 1 ]]; then
    echo "SkillOpt Studio stopped."
else
    echo "SkillOpt Studio is not running."
fi

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "warning: port $PORT is still in use" >&2
    exit 1
fi
