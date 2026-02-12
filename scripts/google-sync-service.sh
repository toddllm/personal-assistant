#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/google-sync-service.pid"
SUPERVISOR_LOG="$ROOT_DIR/data/logs/google-sync-service-supervisor.log"
APP_LOG="$ROOT_DIR/data/logs/google-sync-service.log"

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

APP_CMD=()
if [[ -x "$ROOT_DIR/.venv/bin/google-sync-service" ]]; then
  APP_CMD=("$ROOT_DIR/.venv/bin/google-sync-service")
else
  APP_CMD=("google-sync-service")
fi

health_ok() {
  curl -fsS --max-time 1 "http://127.0.0.1:8792/health" >/dev/null 2>&1
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
  local listener_pid=""
  if [[ ! -f "$PID_FILE" ]]; then
    listener_pid="$(lsof -tiTCP:8792 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    if [[ -n "$listener_pid" ]]; then
      echo "$listener_pid" >"$PID_FILE"
      return 0
    fi
    return 1
  fi

  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    listener_pid="$(lsof -tiTCP:8792 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
    if [[ -n "$listener_pid" ]]; then
      echo "$listener_pid" >"$PID_FILE"
      return 0
    fi
    return 1
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi

  listener_pid="$(lsof -tiTCP:8792 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [[ -n "$listener_pid" ]]; then
    echo "$listener_pid" >"$PID_FILE"
    return 0
  fi
  return 1
}

start_service() {
  if is_running; then
    echo "google-sync-service already running (pid $(cat "$PID_FILE"))."
    return 0
  fi

  local client_secret_path="${GOOGLE_SYNC_CLIENT_SECRET_PATH:-}"
  if [[ -z "$client_secret_path" ]]; then
    local discovered=""
    discovered="$(ls -t "$ROOT_DIR"/google/client_secret_*apps.googleusercontent.com.json 2>/dev/null | head -n 1 || true)"
    if [[ -n "$discovered" ]]; then
      client_secret_path="$discovered"
    fi
  fi

  echo "Starting google-sync-service..."
  if [[ -n "$client_secret_path" ]]; then
    GOOGLE_SYNC_CLIENT_SECRET_PATH="$client_secret_path" nohup "${APP_CMD[@]}" >>"$SUPERVISOR_LOG" 2>&1 &
  else
    nohup "${APP_CMD[@]}" >>"$SUPERVISOR_LOG" 2>&1 &
  fi
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
    echo "google-sync-service started (pid $pid)."
    if wait_for_health 24 0.25; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
    echo "supervisor log: $SUPERVISOR_LOG"
    return 0
  fi
  echo "google-sync-service failed to start. Check $SUPERVISOR_LOG"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "google-sync-service is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping google-sync-service (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "google-sync-service stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing google-sync-service (pid $pid)..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "google-sync-service stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "google-sync-service running (pid $pid)."
    if wait_for_health 8 0.25; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
    return 0
  fi
  echo "google-sync-service not running."
  return 1
}

usage() {
  cat <<'EOF'
Usage: scripts/google-sync-service.sh <command>

Commands:
  start       Start google-sync-service in background
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
