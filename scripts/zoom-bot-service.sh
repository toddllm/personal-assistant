#!/usr/bin/env bash
set -euo pipefail

# Service management for the Zoom AI Bot running in OrbStack VM.
#
# Usage: scripts/zoom-bot-service.sh <command>
#
# Commands:
#   start       Start the zoom-bot service in the VM
#   stop        Stop the service
#   restart     Restart the service
#   status      Show service status and health
#   deploy      Re-deploy code from src/zoom_bot/ into the VM
#   logs        Show recent service logs
#   tail        Tail service logs
#   ssh         Open a shell in the VM

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_NAME="zoom-bot"
DEPLOY_DIR="/opt/zoom-bot"
SERVICE_PORT=8795
SERVICE_URL="http://${VM_NAME}.orb.local:${SERVICE_PORT}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

vm_run() {
  orb -m "$VM_NAME" "$@"
}

vm_exists() {
  orb list -q 2>/dev/null | grep -q "^${VM_NAME}$"
}

check_vm() {
  if ! command -v orb &>/dev/null; then
    echo "Error: OrbStack CLI (orb) not found."
    exit 1
  fi
  if ! vm_exists; then
    echo "Error: VM '$VM_NAME' does not exist. Run scripts/setup-zoom-bot.sh first."
    exit 1
  fi
}

health_ok() {
  curl -fsS --max-time 2 "${SERVICE_URL}/health" >/dev/null 2>&1
}

wait_for_health() {
  local attempts="${1:-20}"
  local delay="${2:-0.5}"
  for ((i = 0; i < attempts; i++)); do
    if health_ok; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

start_service() {
  check_vm

  # Ensure PulseAudio is running
  vm_run sudo bash -c "pulseaudio --check 2>/dev/null || pulseaudio --start --daemonize" || true

  echo "Starting zoom-bot service..."
  vm_run sudo systemctl start zoom-bot.service

  if wait_for_health 20 0.5; then
    echo "zoom-bot started."
    echo "health: ok"
    echo "url: ${SERVICE_URL}"
  else
    echo "zoom-bot started but health check not yet passing."
    echo "Check logs: scripts/zoom-bot-service.sh logs"
  fi
}

stop_service() {
  check_vm
  echo "Stopping zoom-bot service..."
  vm_run sudo systemctl stop zoom-bot.service
  echo "zoom-bot stopped."
}

restart_service() {
  check_vm
  echo "Restarting zoom-bot service..."
  vm_run sudo systemctl restart zoom-bot.service

  if wait_for_health 20 0.5; then
    echo "zoom-bot restarted."
    echo "health: ok"
  else
    echo "zoom-bot restarted but health check not yet passing."
  fi
}

service_status() {
  check_vm

  # systemd status
  echo "=== systemd status ==="
  vm_run sudo systemctl status zoom-bot.service --no-pager 2>/dev/null || true

  echo ""

  # Health check
  echo "=== health ==="
  if health_ok; then
    echo "health: ok"
  else
    echo "health: unreachable"
  fi

  # Bot status
  echo ""
  echo "=== bot status ==="
  local status_json
  status_json=$(curl -sS --max-time 2 "${SERVICE_URL}/status" 2>/dev/null) || true
  if [[ -n "$status_json" ]]; then
    echo "$status_json" | python3 -m json.tool 2>/dev/null || echo "$status_json"
  else
    echo "Could not reach status endpoint."
  fi
}

deploy_code() {
  check_vm

  echo "Deploying zoom-bot code to VM..."
  vm_run sudo bash -c "mkdir -p '$DEPLOY_DIR/zoom_bot'"

  for f in __init__.py main.py audio_pipeline.py sdk_wrapper.py bot_process.py chrome_bot.py; do
    if [[ -f "$ROOT_DIR/src/zoom_bot/$f" ]]; then
      cat "$ROOT_DIR/src/zoom_bot/$f" | vm_run sudo bash -c "cat > '$DEPLOY_DIR/zoom_bot/$f'"
      echo "  deployed $f"
    fi
  done

  echo "Code deployed. Restart with: scripts/zoom-bot-service.sh restart"
}

show_logs() {
  check_vm
  vm_run sudo journalctl -u zoom-bot.service --no-pager -n 100
}

tail_logs() {
  check_vm
  vm_run sudo journalctl -u zoom-bot.service -f
}

open_ssh() {
  check_vm
  orb shell -m "$VM_NAME"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: scripts/zoom-bot-service.sh <command>

Commands:
  start       Start the zoom-bot service in the VM
  stop        Stop the service
  restart     Restart the service
  status      Show service status and health
  deploy      Re-deploy code from src/zoom_bot/ into the VM
  logs        Show recent service logs
  tail        Tail service logs
  ssh         Open a shell in the VM
EOF
}

cmd="${1:-}"
case "$cmd" in
  start)    start_service ;;
  stop)     stop_service ;;
  restart)  restart_service ;;
  status)   service_status ;;
  deploy)   deploy_code ;;
  logs)     show_logs ;;
  tail)     tail_logs ;;
  ssh)      open_ssh ;;
  *)
    usage
    exit 1
    ;;
esac
