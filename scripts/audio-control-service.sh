#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/audio-control.pid"
LOG_FILE="$ROOT_DIR/data/logs/audio-control.log"
SCRIPT="$ROOT_DIR/scripts/audio-control.py"
PYTHON="$ROOT_DIR/.venv/bin/python3"

PORT=8789

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

listener_pid() {
  lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | head -n 1 || true
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

health_ok() {
  curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

start_service() {
  if is_running; then
    echo "audio-control already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Starting audio-control..."
  nohup "$PYTHON" "$SCRIPT" >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  for _ in {1..16}; do
    if is_running; then
      pid="$(cat "$PID_FILE" 2>/dev/null || echo "$pid")"
      break
    fi
    sleep 0.25
  done
  if is_running; then
    echo "audio-control started (pid $pid)."
    echo "http://127.0.0.1:$PORT"
    echo "log: $LOG_FILE"
    return 0
  fi
  echo "audio-control failed to start. Check $LOG_FILE"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "audio-control is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping audio-control (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "audio-control stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing audio-control (pid $pid)..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "audio-control stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "audio-control running (pid $pid)."
    echo "http://127.0.0.1:$PORT"
    if health_ok; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
  else
    echo "audio-control not running."
  fi
  is_running
}

usage() {
  cat <<'EOF'
Usage: scripts/audio-control-service.sh <command>

Commands:
  start       Start audio-control in background
  stop        Stop background process
  restart     Restart service
  status      Show pid and status
  tail        Tail log
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
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  *)
    usage
    exit 1
    ;;
esac
