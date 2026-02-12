#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/audio-assist.pid"
SUPERVISOR_LOG="$ROOT_DIR/data/logs/audio-assist-supervisor.log"
APP_LOG="$ROOT_DIR/data/logs/audio-assist.log"

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

APP_CMD=()
if [[ -x "$ROOT_DIR/.venv/bin/audio-assist" ]]; then
  APP_CMD=("$ROOT_DIR/.venv/bin/audio-assist")
else
  APP_CMD=("audio-assist")
fi

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

start_service() {
  if is_running; then
    echo "audio-assist already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Starting audio-assist..."
  nohup "${APP_CMD[@]}" >>"$SUPERVISOR_LOG" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  sleep 1
  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "audio-assist started (pid $pid)."
    echo "supervisor log: $SUPERVISOR_LOG"
    return 0
  fi
  echo "audio-assist failed to start. Check $SUPERVISOR_LOG"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "audio-assist is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping audio-assist (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "audio-assist stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing audio-assist (pid $pid)..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "audio-assist stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "audio-assist running (pid $pid)."
    curl -sf "http://127.0.0.1:8787/health" >/dev/null 2>&1 && echo "health: ok" || echo "health: unreachable"
    return 0
  fi
  echo "audio-assist not running."
  return 1
}

readiness_status() {
  curl -sS "http://127.0.0.1:8787/v1/capture/readiness" || true
  echo
}

usage() {
  cat <<'EOF'
Usage: scripts/audio-assist-service.sh <command>

Commands:
  start       Start audio-assist in background
  stop        Stop background process
  restart     Restart service
  status      Show pid and health status
  readiness   Print capture readiness JSON
  tail        Tail application log
EOF
}

cmd="${1:-}"
case "$cmd" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  status)
    service_status
    ;;
  readiness)
    readiness_status
    ;;
  tail)
    touch "$APP_LOG"
    tail -n 160 -f "$APP_LOG"
    ;;
  *)
    usage
    exit 1
    ;;
esac
