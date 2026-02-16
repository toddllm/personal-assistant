#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/run/volume-control.pid"
LOG_FILE="$ROOT_DIR/data/logs/volume-control.log"
SCRIPT="$ROOT_DIR/scripts/volume-control.py"
PYTHON="$ROOT_DIR/.venv/bin/python3"

LAUNCHD_LABEL="com.tdeshane.volumecontrol"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"

PORT=8788

# Volume keys daemon (media key interception via CGEventTap)
VKEYS_PID_FILE="$ROOT_DIR/data/run/volume-keys.pid"
VKEYS_LOG="$ROOT_DIR/data/logs/volume-keys.log"
VKEYS_SCRIPT="$ROOT_DIR/scripts/volume-keys.py"

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

vkeys_is_running() {
  [[ -f "$VKEYS_PID_FILE" ]] && kill -0 "$(cat "$VKEYS_PID_FILE")" 2>/dev/null
}

start_vkeys() {
  if vkeys_is_running; then
    echo "volume-keys already running (pid $(cat "$VKEYS_PID_FILE"))."
    return 0
  fi
  echo "Starting volume-keys daemon..."
  PYTHONUNBUFFERED=1 nohup "$PYTHON" "$VKEYS_SCRIPT" >>"$VKEYS_LOG" 2>&1 &
  echo $! >"$VKEYS_PID_FILE"
  echo "volume-keys started (pid $!)."
}

stop_vkeys() {
  if ! vkeys_is_running; then
    rm -f "$VKEYS_PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$VKEYS_PID_FILE")"
  echo "Stopping volume-keys (pid $pid)..."
  kill "$pid" 2>/dev/null || true
  sleep 0.5
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  rm -f "$VKEYS_PID_FILE"
  echo "volume-keys stopped."
}

start_service() {
  if is_running; then
    echo "volume-control already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Starting volume-control..."
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
    echo "volume-control started (pid $pid)."
    echo "http://127.0.0.1:$PORT"
    echo "log: $LOG_FILE"
    start_vkeys
    return 0
  fi
  echo "volume-control failed to start. Check $LOG_FILE"
  rm -f "$PID_FILE"
  return 1
}

remove_launchd_job() {
  if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1; then
    echo "Removing launchd job $LAUNCHD_LABEL..."
    launchctl remove "$LAUNCHD_LABEL" 2>/dev/null || true
    sleep 0.5
  fi
}

stop_service() {
  stop_vkeys
  remove_launchd_job
  if ! is_running; then
    echo "volume-control is not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "Stopping volume-control (pid $pid)..."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      rm -f "$PID_FILE"
      echo "volume-control stopped."
      return 0
    fi
    sleep 0.25
  done
  echo "Force killing volume-control (pid $pid)..."
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  echo "volume-control stopped."
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
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${ROOT_DIR}/data/logs/volume-control.log</string>
  <key>StandardErrorPath</key>
  <string>${ROOT_DIR}/data/logs/volume-control.log</string>
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
    echo "volume-control running (pid $(cat "$PID_FILE"))."
  else
    echo "Waiting for launch..."
    sleep 2
    if is_running; then
      echo "volume-control running (pid $(cat "$PID_FILE"))."
    else
      echo "Check log: $ROOT_DIR/data/logs/volume-control.log"
    fi
  fi
}

uninstall_launchd() {
  remove_launchd_job
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
    echo "volume-control running (pid $pid)."
    echo "http://127.0.0.1:$PORT"
  else
    echo "volume-control not running."
  fi
  if vkeys_is_running; then
    echo "volume-keys running (pid $(cat "$VKEYS_PID_FILE"))."
  else
    echo "volume-keys not running."
  fi
  is_running
}

usage() {
  cat <<'EOF'
Usage: scripts/volume-control-service.sh <command>

Commands:
  start       Start volume-control in background
  stop        Stop background process
  restart     Restart service
  status      Show pid and status
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
