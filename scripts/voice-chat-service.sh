#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/voice-chat.pid"
LOG_FILE="$ROOT_DIR/data/logs/voice-chat.log"
PYTHON="$ROOT_DIR/.venv/bin/python3"
MODULE="voice_chat.service"

PORT=8797

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

export PYTHONPATH="$ROOT_DIR/src"

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

start_service() {
  if is_running; then
    echo "voice-chat already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Starting voice-chat..."
  nohup "$PYTHON" -m "$MODULE" >>"$LOG_FILE" 2>&1 &
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
    echo "voice-chat started (pid $pid)."
    echo "http://127.0.0.1:$PORT"
    echo "log: $LOG_FILE"
    return 0
  fi
  echo "voice-chat failed to start. Check $LOG_FILE"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "voice-chat is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping voice-chat (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "voice-chat stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing voice-chat (pid $pid)..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "voice-chat stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "voice-chat running (pid $pid)."
    echo "http://127.0.0.1:$PORT"
    return 0
  fi
  echo "voice-chat not running."
  return 1
}

usage() {
  cat <<'EOF'
Usage: scripts/voice-chat-service.sh <command>

Commands:
  start       Start voice-chat in background
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
