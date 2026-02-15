#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/audio-assist.pid"
SUPERVISOR_LOG="$ROOT_DIR/data/logs/audio-assist-supervisor.log"
APP_LOG="$ROOT_DIR/data/logs/audio-assist.log"
LAUNCHD_LABEL="com.tdeshane.audioassist"

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

APP_CMD=()
if [[ -x "$ROOT_DIR/.venv/bin/audio-assist" ]]; then
  APP_CMD=("$ROOT_DIR/.venv/bin/audio-assist")
else
  APP_CMD=("audio-assist")
fi

listener_pid() {
  lsof -tiTCP:8787 -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

list_audio_assist_pids() {
  ps -axo pid=,command= | awk '$0 !~ /audio-assist-service.sh/ && ($0 ~ /[.]venv\/bin\/audio-assist/ || $0 ~ /(^| )audio-assist( |$)/) {print $1}' | tr '\n' ' '
}

health_ok() {
  curl -fsS --max-time 1 "http://127.0.0.1:8787/health" >/dev/null 2>&1
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
    echo "audio-assist already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  local stale_pids
  stale_pids="$(list_audio_assist_pids)"
  if [[ -n "${stale_pids// }" ]]; then
    echo "Cleaning up stale audio-assist process(es): ${stale_pids}" 
    for pid in $stale_pids; do
      kill "$pid" >/dev/null 2>&1 || true
    done
    sleep 0.3
  fi
  echo "Starting audio-assist..."
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
    echo "audio-assist started (pid $pid)."
    if wait_for_health 24 0.25; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
    echo "supervisor log: $SUPERVISOR_LOG"
    return 0
  fi
  echo "audio-assist failed to start. Check $SUPERVISOR_LOG"
  rm -f "$PID_FILE"
  return 1
}

remove_launchd_job() {
  if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
    echo "Removing launchd job $LAUNCHD_LABEL (prevents auto-restart)..."
    launchctl remove "$LAUNCHD_LABEL" 2>/dev/null || true
    sleep 0.5
  fi
}

stop_service() {
  remove_launchd_job
  local target_pids=""
  if is_running; then
    target_pids="$(cat "$PID_FILE" 2>/dev/null || true)"
  fi
  local discovered_pids
  discovered_pids="$(list_audio_assist_pids)"
  for pid in $discovered_pids; do
    case " $target_pids " in
      *" $pid "*) ;;
      *) target_pids="$target_pids $pid" ;;
    esac
  done
  if [[ -z "${target_pids// }" ]]; then
    echo "audio-assist is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  echo "Stopping audio-assist (pid(s):${target_pids})..."
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
      echo "audio-assist stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing audio-assist (pid(s):${target_pids})..."
  for pid in $target_pids; do
    kill -9 "$pid" >/dev/null 2>&1 || true
  done
  rm -f "$PID_FILE"
  echo "audio-assist stopped."
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "audio-assist running (pid $pid)."
    if wait_for_health 8 0.25; then
      echo "health: ok"
    else
      echo "health: warming"
    fi
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
