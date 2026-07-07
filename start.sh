#!/usr/bin/env bash
# Start SkillOpt Studio (FastAPI + built React frontend) in the background.
#
#   ./start.sh                 # dev:  http://127.0.0.1:8321, no auth
#   ./start.sh --prod          # prod: 0.0.0.0:8321, username/password login
#                              #       (credentials auto-generated on first run
#                              #        → outputs/studio/auth_password)
#   STUDIO_PORT=8400 ./start.sh
#
# Prod credentials: STUDIO_AUTH_USERNAME (default: admin) and
# STUDIO_AUTH_PASSWORD (default: generated once, persisted to
# outputs/studio/auth_password). Set them in the shell or in .env to override.
# Model-gateway env vars must be in this shell (or in .env, which is sourced).
set -euo pipefail
cd "$(dirname "$0")"

MODE="dev"
[[ "${1:-}" == "--prod" ]] && MODE="prod"

PORT="${STUDIO_PORT:-8321}"
if [[ "$MODE" == "prod" ]]; then
    HOST="${STUDIO_HOST:-0.0.0.0}"
else
    HOST="${STUDIO_HOST:-127.0.0.1}"
fi
CHECK_HOST="127.0.0.1"
PID_FILE="outputs/studio/studio.pid"
LOG_FILE="outputs/studio/studio.log"
FRONTEND="skillopt_studio/frontend"

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
# Loaded before prod credential handling so STUDIO_AUTH_* may live in .env.
if [[ -f .env ]]; then
    set -a; source .env; set +a
fi

mkdir -p outputs/studio

# One-time frontend build if dist/ is missing.
if [[ ! -f "$FRONTEND/dist/index.html" ]]; then
    echo "frontend dist/ missing — building once (npm install && npm run build)..."
    (cd "$FRONTEND" && npm install --no-audit --no-fund && npm run build)
fi

# ── Prod: rebuild stale frontend + require login credentials ────────────────
if [[ "$MODE" == "prod" ]]; then
    # Rebuild when any frontend source is newer than the built index.html.
    if [[ -n "$(find "$FRONTEND/src" "$FRONTEND/index.html" "$FRONTEND/vite.config.ts" \
        -newer "$FRONTEND/dist/index.html" -print -quit 2>/dev/null)" ]]; then
        echo "frontend sources newer than dist/ — rebuilding..."
        (cd "$FRONTEND" && npm run build)
    fi

    PASS_FILE="outputs/studio/auth_password"
    if [[ -n "${STUDIO_AUTH_PASSWORD:-}" ]]; then
        printf '%s' "$STUDIO_AUTH_PASSWORD" >"$PASS_FILE"
        chmod 600 "$PASS_FILE"
    elif [[ ! -s "$PASS_FILE" ]]; then
        openssl rand -base64 18 | tr -d '/+=' | head -c 20 >"$PASS_FILE"
        chmod 600 "$PASS_FILE"
        echo "generated access password → $PASS_FILE"
    fi
    # First line only — the file may carry human notes below the password.
    export STUDIO_AUTH_PASSWORD="$(head -n 1 "$PASS_FILE")"
    export STUDIO_AUTH_USERNAME="${STUDIO_AUTH_USERNAME:-admin}"
fi

nohup python3 -m skillopt_studio --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

for _ in $(seq 1 30); do
    if curl -sf "http://$CHECK_HOST:$PORT/api/health" >/dev/null 2>&1; then
        echo "SkillOpt Studio running ($MODE) — http://$HOST:$PORT/  (pid $(cat "$PID_FILE"), log: $LOG_FILE)"
        if [[ "$MODE" == "prod" ]]; then
            echo "login:    user '$STUDIO_AUTH_USERNAME', password in outputs/studio/auth_password"
        fi
        exit 0
    fi
    sleep 0.5
done

echo "error: server did not become healthy within 15s; last log lines:" >&2
tail -20 "$LOG_FILE" >&2 || true
rm -f "$PID_FILE"
exit 1
