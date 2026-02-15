#!/usr/bin/env bash
# Install the CaptureAudio virtual audio driver.
# Usage: scripts/install-driver.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DRIVER_DIR="$REPO_DIR/driver"

echo "==> Building and installing CaptureAudio driver..."
make -C "$DRIVER_DIR" install

echo ""
echo "==> Verifying device appeared..."
sleep 1

# Try SwitchAudioSource if available, otherwise fall back to system_profiler
if command -v SwitchAudioSource &>/dev/null; then
    if SwitchAudioSource -a 2>/dev/null | grep -q "CaptureAudio"; then
        echo "OK: CaptureAudio device found:"
        SwitchAudioSource -a | grep "CaptureAudio"
    else
        echo "WARNING: CaptureAudio device not found in SwitchAudioSource output."
        echo "Try: SwitchAudioSource -a"
        exit 1
    fi
else
    echo "SwitchAudioSource not installed — checking with system_profiler..."
    if system_profiler SPAudioDataType 2>/dev/null | grep -q "CaptureAudio"; then
        echo "OK: CaptureAudio device found in system_profiler output."
    else
        echo "WARNING: CaptureAudio device not found. Check Audio MIDI Setup."
        exit 1
    fi
fi

echo ""
echo "==> Done. Add 'CaptureAudio 2ch' to your Multi-Output Device in Audio MIDI Setup."
