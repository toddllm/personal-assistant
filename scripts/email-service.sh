#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/email-service.pid"
SUPERVISOR_LOG="$ROOT_DIR/data/logs/email-service-supervisor.log"
APP_LOG="$ROOT_DIR/data/logs/email-service.log"

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

APP_CMD=()
if [[ -x "$ROOT_DIR/.venv/bin/email-service" ]]; then
  APP_CMD=("$ROOT_DIR/.venv/bin/email-service")
else
  APP_CMD=("email-service")
fi

is_running() {
  local command=""
  local listener_pid=""
  if [[ ! -f "$PID_FILE" ]]; then
    listener_pid="$(lsof -tiTCP:8793 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    if [[ -n "$listener_pid" ]]; then
      command="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
      if [[ "$command" == *"email-service"* ]]; then
        echo "$listener_pid" >"$PID_FILE"
        return 0
      fi
    fi
    return 1
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    listener_pid="$(lsof -tiTCP:8793 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    if [[ -n "$listener_pid" ]]; then
      command="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
      if [[ "$command" == *"email-service"* ]]; then
        echo "$listener_pid" >"$PID_FILE"
        return 0
      fi
    fi
    return 1
  fi
  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi
  listener_pid="$(lsof -tiTCP:8793 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [[ -n "$listener_pid" ]]; then
    command="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
    if [[ "$command" == *"email-service"* ]]; then
      echo "$listener_pid" >"$PID_FILE"
      return 0
    fi
  fi
  return 1
}

start_service() {
  if is_running; then
    echo "email-service already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Starting email-service..."
  nohup "${APP_CMD[@]}" >>"$SUPERVISOR_LOG" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  for _ in {1..24}; do
    if is_running; then
      pid="$(cat "$PID_FILE" 2>/dev/null || echo "$pid")"
      break
    fi
    sleep 0.25
  done
  if is_running; then
    echo "email-service started (pid $pid)."
    echo "supervisor log: $SUPERVISOR_LOG"
    return 0
  fi
  echo "email-service failed to start. Check $SUPERVISOR_LOG"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "email-service is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping email-service (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "email-service stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing email-service (pid $pid)..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "email-service stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "email-service running (pid $pid)."
    curl -sf "http://127.0.0.1:8793/health" >/dev/null 2>&1 && echo "health: ok" || echo "health: unreachable"
    return 0
  fi
  echo "email-service not running."
  return 1
}

usage() {
  cat <<'EOF'
Usage: scripts/email-service.sh <command>

Commands:
  start       Start email-service in background
  stop        Stop background process
  restart     Restart service
  status      Show pid and health status
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
  tail)
    touch "$APP_LOG"
    tail -n 160 -f "$APP_LOG"
    ;;
  *)
    usage
    exit 1
    ;;
esac
