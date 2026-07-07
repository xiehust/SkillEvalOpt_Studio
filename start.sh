#!/usr/bin/env bash
# Start SkillOpt Studio (FastAPI + built React frontend) in the background.
#   ./start.sh                 # http://127.0.0.1:8321
#   STUDIO_PORT=8400 ./start.sh
# Model-gateway env vars must be in this shell (or in .env, which is sourced).
set -euo pipefail
cd "$(dirname "$0")"

HOST="${STUDIO_HOST:-127.0.0.1}"
PORT="${STUDIO_PORT:-8321}"
PID_FILE="outputs/studio/studio.pid"
LOG_FILE="outputs/studio/studio.log"

# Already running under our pidfile?
if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE")"
    if kill -0 "$PID" 2>/dev/null && ps -p "$PID" -o args= | grep -q skillopt_studio; then
        echo "SkillOpt Studio already running (pid $PID) — http://$HOST:$PORT/"
        exit 0
    fi
    rm -f "$PID_FILE"  # stale pidfile
fi

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    echo "error: port $PORT is already in use by another process" >&2
    exit 1
fi

# Backend/env config convention (see CLAUDE.md): load .env when present.
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

# One-time frontend build if dist/ is missing.
if [[ ! -f skillopt_studio/frontend/dist/index.html ]]; then
    echo "frontend dist/ missing — building once (npm install && npm run build)..."
    (cd skillopt_studio/frontend && npm install --no-audit --no-fund && npm run build)
fi

mkdir -p outputs/studio
nohup python3 -m skillopt_studio --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

for _ in $(seq 1 30); do
    if curl -sf "http://$HOST:$PORT/api/health" >/dev/null 2>&1; then
        echo "SkillOpt Studio running — http://$HOST:$PORT/  (pid $(cat "$PID_FILE"), log: $LOG_FILE)"
        exit 0
    fi
    sleep 0.5
done

echo "error: server did not become healthy within 15s; last log lines:" >&2
tail -20 "$LOG_FILE" >&2 || true
rm -f "$PID_FILE"
exit 1
