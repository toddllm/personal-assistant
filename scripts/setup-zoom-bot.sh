#!/usr/bin/env bash
set -euo pipefail

# Setup script for the Zoom AI Bot OrbStack VM.
#
# Creates an amd64 Ubuntu 22.04 VM via OrbStack, installs all dependencies
# (Python 3.11, PulseAudio, Zoom Meeting SDK, Deepgram/Groq/ElevenLabs clients),
# and deploys the zoom-bot service.
#
# Usage: scripts/setup-zoom-bot.sh [--force]
#   --force   Destroy and recreate the VM from scratch

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_NAME="zoom-bot"
DEPLOY_DIR="/opt/zoom-bot"
VENV_DIR="$DEPLOY_DIR/venv"
SERVICE_PORT=8795
FORCE=false

if [[ "${1:-}" == "--force" ]]; then
  FORCE=true
fi

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

if ! command -v orb &>/dev/null; then
  echo "Error: OrbStack CLI (orb) not found. Install from https://orbstack.dev"
  exit 1
fi

# ---------------------------------------------------------------------------
# Create VM
# ---------------------------------------------------------------------------

vm_exists() {
  orb list -q 2>/dev/null | grep -q "^${VM_NAME}$"
}

if $FORCE && vm_exists; then
  echo "Destroying existing VM '$VM_NAME'..."
  orb delete "$VM_NAME" -f
fi

if ! vm_exists; then
  echo "Creating amd64 Ubuntu 22.04 VM '$VM_NAME'..."
  orb create -a amd64 ubuntu:jammy "$VM_NAME"
  echo "VM created. Waiting for boot..."
  sleep 5
else
  echo "VM '$VM_NAME' already exists."
fi

# Helper: run a command inside the VM
vm_run() {
  orb -m "$VM_NAME" "$@"
}

# ---------------------------------------------------------------------------
# Install system packages
# ---------------------------------------------------------------------------

echo ""
echo "Installing system packages..."
vm_run sudo bash -c "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    pulseaudio pulseaudio-utils \
    build-essential pkg-config \
    libssl-dev libffi-dev \
    libgl1-mesa-glx libglib2.0-0 \
    curl wget git \
    xvfb \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2
"

# ---------------------------------------------------------------------------
# Configure PulseAudio for headless operation
# ---------------------------------------------------------------------------

echo ""
echo "Configuring PulseAudio (headless)..."
vm_run sudo bash -c "
  mkdir -p /root/.config/pulse

  # Virtual sinks/sources for both SDK and Chrome bot modes
  cat > /root/.config/pulse/default.pa << 'PULSEEOF'
.include /etc/pulse/default.pa

# Null sink for meeting output (Chrome plays here; monitor used for STT capture)
load-module module-null-sink sink_name=virtual_output sink_properties=device.description=MeetingOutput

# Note: tts_sink and chrome_mic no longer needed — Chrome bot injects TTS audio
# directly into WebRTC via WebSocket + Web Audio API, bypassing PulseAudio.

# Legacy sinks for SDK mode
load-module module-null-sink sink_name=zoom_output sink_properties=device.description=ZoomOutput
load-module module-null-sink sink_name=zoom_input sink_properties=device.description=ZoomInput

set-default-sink virtual_output
PULSEEOF

  # Start PulseAudio if not running
  pulseaudio --check 2>/dev/null || pulseaudio --start --daemonize || true
"

# ---------------------------------------------------------------------------
# Create deploy directory and virtualenv
# ---------------------------------------------------------------------------

echo ""
echo "Setting up Python environment..."
vm_run sudo bash -c "
  mkdir -p '$DEPLOY_DIR'
  python3.11 -m venv '$VENV_DIR'
  '$VENV_DIR/bin/pip' install --quiet --upgrade pip setuptools wheel
"

# ---------------------------------------------------------------------------
# Install Python packages
# ---------------------------------------------------------------------------

echo ""
echo "Installing Python packages..."
vm_run sudo bash -c "
  '$VENV_DIR/bin/pip' install --quiet \
    'zoom-meeting-sdk>=0.0.25' \
    'deepgram-sdk>=3.0' \
    'websockets>=12.0' \
    'httpx>=0.27' \
    'fastapi>=0.110' \
    'uvicorn[standard]>=0.29' \
    'pydantic>=2.0' \
    'numpy>=1.26' \
    'playwright>=1.40'
"

# ---------------------------------------------------------------------------
# Install Chromium for Playwright (Chrome bot fallback)
# ---------------------------------------------------------------------------

echo ""
echo "Installing Chromium via Playwright..."
vm_run sudo bash -c "
  '$VENV_DIR/bin/playwright' install chromium --with-deps 2>/dev/null || \
    echo 'Note: Playwright Chromium install failed (may need manual install)'
"

# ---------------------------------------------------------------------------
# Deploy service code
# ---------------------------------------------------------------------------

echo ""
echo "Deploying zoom-bot service code..."

# Copy the zoom_bot package
vm_run sudo bash -c "mkdir -p '$DEPLOY_DIR/zoom_bot'"

for f in __init__.py main.py audio_pipeline.py sdk_wrapper.py bot_process.py chrome_bot.py; do
  if [[ -f "$ROOT_DIR/src/zoom_bot/$f" ]]; then
    cat "$ROOT_DIR/src/zoom_bot/$f" | vm_run sudo bash -c "cat > '$DEPLOY_DIR/zoom_bot/$f'"
  fi
done

# ---------------------------------------------------------------------------
# Create .env template if credentials file doesn't exist
# ---------------------------------------------------------------------------

vm_run sudo bash -c "
  if [[ ! -f '$DEPLOY_DIR/.env' ]]; then
    cat > '$DEPLOY_DIR/.env' << 'ENVEOF'
# Zoom AI Bot credentials — fill these in
ZOOM_APP_CLIENT_ID=
ZOOM_APP_CLIENT_SECRET=
DEEPGRAM_API_KEY=
GROQ_API_KEY=
ELEVENLABS_API_KEY=
ENVEOF
    echo 'Created $DEPLOY_DIR/.env — fill in your API keys'
  else
    echo '$DEPLOY_DIR/.env already exists, preserving credentials'
  fi
"

# Symlink .env into the package directory
vm_run sudo bash -c "ln -sf '$DEPLOY_DIR/.env' '$DEPLOY_DIR/zoom_bot/.env'"

# ---------------------------------------------------------------------------
# Create systemd service
# ---------------------------------------------------------------------------

echo ""
echo "Creating systemd service..."
vm_run sudo bash -c "
  cat > /etc/systemd/system/zoom-bot.service << 'SVCEOF'
[Unit]
Description=Zoom AI Bot Service
After=network.target pulseaudio.service

[Service]
Type=simple
WorkingDirectory=$DEPLOY_DIR
Environment=PYTHONPATH=$DEPLOY_DIR
ExecStart=$VENV_DIR/bin/uvicorn zoom_bot.main:app --host 0.0.0.0 --port $SERVICE_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

  systemctl daemon-reload
  systemctl enable zoom-bot.service
"

# ---------------------------------------------------------------------------
# Verify installation
# ---------------------------------------------------------------------------

echo ""
echo "Verifying installation..."

vm_run sudo bash -c "
  '$VENV_DIR/bin/python' -c '
import fastapi, httpx, pydantic, uvicorn
print(\"Core packages: OK\")
' 2>&1
" || echo "Warning: Some packages may not have installed correctly"

# Check if Zoom SDK is available (may fail on first setup if wheels aren't published)
vm_run sudo bash -c "
  '$VENV_DIR/bin/python' -c 'import zoom_meeting_sdk; print(\"Zoom SDK: OK\")' 2>&1
" || echo "Note: zoom-meeting-sdk not available yet — install manually when wheels are published"

echo ""
echo "========================================"
echo "Setup complete!"
echo "========================================"
echo ""
echo "VM:       $VM_NAME (amd64 Ubuntu 22.04)"
echo "Deploy:   $DEPLOY_DIR"
echo "Service:  http://${VM_NAME}.orb.local:${SERVICE_PORT}"
echo ""
echo "Next steps:"
echo "  1. Fill in API keys: orb -m $VM_NAME nano $DEPLOY_DIR/.env"
echo "  2. Start the service: scripts/zoom-bot-service.sh start"
echo "  3. Test: curl http://${VM_NAME}.orb.local:${SERVICE_PORT}/health"
echo ""
