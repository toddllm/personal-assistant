#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/screen-capture.pid"
SUPERVISOR_LOG="$ROOT_DIR/data/logs/screen-capture-supervisor.log"
APP_LOG="$ROOT_DIR/data/logs/screen-capture.log"

PORT=8794

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

APP_CMD=()
if [[ -x "$ROOT_DIR/.venv/bin/screen-capture-service" ]]; then
  APP_CMD=("$ROOT_DIR/.venv/bin/screen-capture-service")
else
  APP_CMD=("screen-capture-service")
fi

listener_pid() {
  lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

list_service_pids() {
  ps -axo pid=,command= | awk '$0 !~ /screen-capture-service.sh/ && ($0 ~ /[.]venv\/bin\/screen-capture-service/ || $0 ~ /(^| )screen-capture-service( |$)/) {print $1}' | tr '\n' ' '
}

health_ok() {
  curl -fsS --max-time 1 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
}

wait_for_health() {
  local attempts="${1:-16}"
  local delay="${2:-0.25}"
  local i
  for ((i = 0; i < attempts; i++)); do
    if health_ok; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

is_running() {
  local pid
  pid="$(listener_pid)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  echo "$pid" >"$PID_FILE"
  return 0
}

start_service() {
  if is_running; then
    echo "screen-capture-service already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  local stale_pids
  stale_pids="$(list_service_pids)"
  if [[ -n "${stale_pids// }" ]]; then
    echo "Cleaning up stale screen-capture-service process(es): ${stale_pids}"
    for pid in $stale_pids; do
      kill "$pid" >/dev/null 2>&1 || true
    done
    sleep 0.3
  fi
  echo "Starting screen-capture-service..."
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
    echo "screen-capture-service started (pid $pid)."
    if wait_for_health 24 0.25; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
    echo "supervisor log: $SUPERVISOR_LOG"
    return 0
  fi
  echo "screen-capture-service failed to start. Check $SUPERVISOR_LOG"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  local target_pids=""
  if is_running; then
    target_pids="$(cat "$PID_FILE" 2>/dev/null || true)"
  fi
  local discovered_pids
  discovered_pids="$(list_service_pids)"
  for pid in $discovered_pids; do
    case " $target_pids " in
      *" $pid "*) ;;
      *) target_pids="$target_pids $pid" ;;
    esac
  done
  if [[ -z "${target_pids// }" ]]; then
    echo "screen-capture-service is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  echo "Stopping screen-capture-service (pid(s):${target_pids})..."
  for pid in $target_pids; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for _ in {1..20}; do
    local still_running=0
    for pid in $target_pids; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        still_running=1
        break
      fi
    done
    if [[ "$still_running" -eq 0 ]]; then
      rm -f "$PID_FILE"
      echo "screen-capture-service stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing screen-capture-service (pid(s):${target_pids})..."
  for pid in $target_pids; do
    kill -9 "$pid" >/dev/null 2>&1 || true
  done
  rm -f "$PID_FILE"
  echo "screen-capture-service stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "screen-capture-service running (pid $pid)."
    if wait_for_health 8 0.25; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
    return 0
  fi
  echo "screen-capture-service not running."
  return 1
}

usage() {
  cat <<'EOF'
Usage: scripts/screen-capture-service.sh <command>

Commands:
  start       Start screen-capture-service in background
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
