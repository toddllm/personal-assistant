#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/mic-forward.pid"
LOG_FILE="$ROOT_DIR/data/logs/mic-forward.log"
PYTHON="$ROOT_DIR/.venv/bin/python3"
SCRIPT="$ROOT_DIR/scripts/mic-forward.py"

LAUNCHD_LABEL="com.tdeshane.micforward"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

mkdir -p "$ROOT_DIR/data/run" "$ROOT_DIR/data/logs"

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  # Also check if the script is running even without PID file
  local pid
  pid="$(pgrep -f "mic-forward.py" 2>/dev/null | head -1 || true)"
  if [[ -n "$pid" ]]; then
    echo "$pid" > "$PID_FILE"
    return 0
  fi
  return 1
}

start_service() {
  if is_running; then
    echo "mic-forward already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Starting mic-forward (Python multi-mic mixer)..."
  nohup "$PYTHON" "$SCRIPT" >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  sleep 1
  if is_running; then
    echo "mic-forward started (pid $pid)."
    echo "log: $LOG_FILE"
    return 0
  fi
  echo "mic-forward failed to start. Check $LOG_FILE"
  rm -f "$PID_FILE"
  return 1
}

stop_service() {
  if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
    echo "Removing launchd job $LAUNCHD_LABEL..."
    launchctl remove "$LAUNCHD_LABEL" 2>/dev/null || true
    sleep 0.5
  fi
  # Also kill any Python mic-forward.py that might be running
  pkill -f "mic-forward.py" 2>/dev/null || true
  if ! is_running; then
    echo "mic-forward is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping mic-forward (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "mic-forward stopped."
      return 0
    fi
    sleep 0.25
  done
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "mic-forward stopped (forced)."
}

install_launchd() {
  mkdir -p "$(dirname "$LAUNCHD_PLIST")"
  cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SCRIPT}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>${ROOT_DIR}/src</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${ROOT_DIR}/data/logs/mic-forward.log</string>
  <key>StandardErrorPath</key>
  <string>${ROOT_DIR}/data/logs/mic-forward.log</string>
  <key>WorkingDirectory</key>
  <string>${ROOT_DIR}</string>
</dict>
</plist>
EOF
  echo "Installed $LAUNCHD_PLIST"
  launchctl load "$LAUNCHD_PLIST"
  echo "Loaded launchd job $LAUNCHD_LABEL (will start at login)."
  sleep 1
  if is_running; then
    echo "mic-forward running (pid $(cat "$PID_FILE"))."
  else
    sleep 2
    if is_running; then
      echo "mic-forward running (pid $(cat "$PID_FILE"))."
    else
      echo "Check log: $LOG_FILE"
    fi
  fi
}

uninstall_launchd() {
  if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
    launchctl remove "$LAUNCHD_LABEL" 2>/dev/null || true
  fi
  if [[ -f "$LAUNCHD_PLIST" ]]; then
    launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
    rm -f "$LAUNCHD_PLIST"
    echo "Removed $LAUNCHD_PLIST"
  else
    echo "No plist found at $LAUNCHD_PLIST"
  fi
}

service_status() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "mic-forward running (pid $pid)."
    return 0
  fi
  echo "mic-forward not running."
  return 1
}

usage() {
  cat <<'EOF'
Usage: scripts/mic-forward-service.sh <command>

Routes all physical mic audio to CaptureMic 2ch virtual device using
the Python multi-mic mixer with auto-discovery.

Commands:
  start       Start mic-forward daemon
  stop        Stop daemon
  restart     Restart daemon
  status      Show whether daemon is running
  install     Install launchd plist (start at login)
  uninstall   Remove launchd plist
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
  install)
    install_launchd
    ;;
  uninstall)
    stop_service
    uninstall_launchd
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
