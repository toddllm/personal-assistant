#!/usr/bin/env bash
# audio-passthrough.sh — reads from BlackHole 16ch and plays to default output
# so the user can hear app audio (e.g. WhatsApp calls) routed through the
# capture device.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/audio-passthrough.pid"
LOG_FILE="$ROOT_DIR/data/logs/audio-passthrough.log"

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

DEVICE_NAME="${AUDIO_PASSTHROUGH_DEVICE:-BlackHole 16ch}"

device_exists() {
  SwitchAudioSource -a -t output 2>/dev/null | grep -qF "$DEVICE_NAME"
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_passthrough() {
  if is_running; then
    echo "audio-passthrough already running (pid $(cat "$PID_FILE"))."
    return 0
  fi

  if ! device_exists; then
    echo "Device '$DEVICE_NAME' not found. Install blackhole-16ch:"
    echo "  brew install blackhole-16ch"
    return 1
  fi

  echo "Starting audio-passthrough ($DEVICE_NAME → default output)..."
  nohup ffmpeg -hide_banner -loglevel warning \
    -f avfoundation -i "none:$DEVICE_NAME" \
    -f audiotoolbox - \
    >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"

  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    echo "audio-passthrough started (pid $pid)."
    echo "log: $LOG_FILE"
  else
    echo "audio-passthrough failed to start. Check $LOG_FILE"
    rm -f "$PID_FILE"
    return 1
  fi
}

stop_passthrough() {
  if ! is_running; then
    echo "audio-passthrough is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  echo "Stopping audio-passthrough (pid $pid)..."
  kill "$pid" 2>/dev/null || true
  for _ in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "audio-passthrough stopped."
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "audio-passthrough stopped (forced)."
}

status_passthrough() {
  if is_running; then
    echo "audio-passthrough running (pid $(cat "$PID_FILE"))."
  else
    echo "audio-passthrough not running."
    return 1
  fi
}

usage() {
  cat <<'EOF'
Usage: scripts/audio-passthrough.sh <command>

Routes audio from BlackHole 16ch to the default output device so
the user can hear app audio (e.g. WhatsApp calls) while it is
simultaneously captured for transcription.

Commands:
  start    Start the passthrough daemon
  stop     Stop the passthrough daemon
  restart  Restart the passthrough daemon
  status   Show whether passthrough is running
EOF
}

cmd="${1:-}"
case "$cmd" in
  start)
    start_passthrough
    ;;
  stop)
    stop_passthrough
    ;;
  restart)
    stop_passthrough
    start_passthrough
    ;;
  status)
    status_passthrough
    ;;
  *)
    usage
    exit 1
    ;;
esac
