#!/usr/bin/env bash
# Uninstall the CaptureAudio virtual audio driver.
# Usage: scripts/uninstall-driver.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DRIVER_DIR="$REPO_DIR/driver"

echo "==> Uninstalling CaptureAudio driver..."
make -C "$DRIVER_DIR" uninstall

echo ""
echo "==> Verifying device removed..."
sleep 1

if command -v SwitchAudioSource &>/dev/null; then
    if SwitchAudioSource -a 2>/dev/null | grep -q "CaptureAudio"; then
        echo "WARNING: CaptureAudio device still appears. You may need to restart."
    else
        echo "OK: CaptureAudio device successfully removed."
    fi
else
    echo "SwitchAudioSource not installed — check Audio MIDI Setup manually."
fi

echo "==> Done."
