#!/usr/bin/env bash
# check-audio-routing.sh — Verify audio routing matches the planned architecture.
# See docs/audio-routing-architecture.md for the full spec.
#
# Exit codes: 0 = all OK, 1 = warnings, 2 = critical issues
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT_DIR}/.venv/bin/python"

RED='\033[0;31m'
YEL='\033[0;33m'
GRN='\033[0;32m'
DIM='\033[0;2m'
RST='\033[0m'

warnings=0
errors=0

ok()   { echo -e "  ${GRN}OK${RST}  $1"; }
warn() { echo -e "  ${YEL}WARN${RST} $1"; ((warnings++)); }
fail() { echo -e "  ${RED}FAIL${RST} $1"; ((errors++)); }
info() { echo -e "  ${DIM}----${RST} $1"; }

echo "=== Audio Routing Health Check ==="
echo ""

# ─── 1. Virtual drivers loaded ────────────────────────────────────────────
echo "Virtual Drivers:"
for driver in CaptureAudio2ch CaptureMic2ch; do
  if [[ -d "/Library/Audio/Plug-Ins/HAL/${driver}.driver" ]]; then
    ok "$driver.driver installed"
  else
    fail "$driver.driver NOT installed in /Library/Audio/Plug-Ins/HAL/"
  fi
done

# Check if devices are visible to CoreAudio
if $PYTHON -c "
import sounddevice as sd
devs = [d['name'] for d in sd.query_devices()]
assert 'CaptureAudio 2ch' in devs, 'CaptureAudio 2ch not in device list'
assert 'CaptureMic 2ch' in devs, 'CaptureMic 2ch not in device list'
" 2>/dev/null; then
  ok "Both virtual devices visible to CoreAudio"
else
  fail "Virtual devices NOT visible (coreaudiod may need restart)"
fi
echo ""

# ─── 2. Bluetooth / Bose state ───────────────────────────────────────────
echo "Bluetooth (Bose QC45):"
bose_info=$($PYTHON -c "
import sounddevice as sd
for d in sd.query_devices():
    if 'bose' in d['name'].lower() and d['max_output_channels'] >= 2:
        print(f'{d[\"default_samplerate\"]:.0f}')
        break
else:
    print('disconnected')
" 2>/dev/null)

if [[ "$bose_info" == "disconnected" ]]; then
  info "Bose QC45 not connected (OK if not using headphones)"
elif [[ "$bose_info" == "44100" ]]; then
  ok "Bose QC45 in A2DP mode (44100 Hz stereo)"
elif [[ "$bose_info" == "16000" ]]; then
  fail "Bose QC45 in HFP mode (16000 Hz mono) — output will be muddy"
  info "Fix: blueutil --disconnect AC:BF:71:69:17:48 && sleep 3 && blueutil --connect AC:BF:71:69:17:48"
else
  warn "Bose QC45 at unexpected rate: ${bose_info} Hz"
fi

# Check mic-settings.json: Bose must be disabled
if [[ -f "$ROOT_DIR/data/mic-settings.json" ]]; then
  bose_enabled=$(python3 -c "
import json
with open('$ROOT_DIR/data/mic-settings.json') as f:
    s = json.load(f)
b = s.get('bose-qc45', {})
print('true' if b.get('enabled', True) and b.get('volume', 100) > 0 else 'false')
" 2>/dev/null)
  if [[ "$bose_enabled" == "false" ]]; then
    ok "Bose mic disabled in mic-settings.json (prevents HFP)"
  else
    fail "Bose mic ENABLED in mic-settings.json — will force HFP mode!"
    info "Fix: set bose-qc45 to {\"volume\": 0, \"enabled\": false} in data/mic-settings.json"
  fi
else
  warn "data/mic-settings.json not found"
fi
echo ""

# ─── 3. Services running ─────────────────────────────────────────────────
echo "Services:"

check_port() {
  local name="$1" port="$2"
  if lsof -i ":${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    ok "$name listening on port $port"
  else
    warn "$name NOT running on port $port"
  fi
}

check_port "audio-assist" 8787
check_port "volume-control" 8788
check_port "discord-bot" 8796
check_port "voice-chat" 8797

# Check daemons (no port, use process name)
if pgrep -f "driver/build/audio-forward" >/dev/null 2>&1; then
  ok "audio-forward daemon running (C)"
elif pgrep -f "audio-forward.py" >/dev/null 2>&1; then
  warn "audio-forward daemon running (Python — high latency, use C version)"
else
  warn "audio-forward daemon NOT running"
fi

if pgrep -f "mic-forward.py" >/dev/null 2>&1; then
  ok "mic-forward (Python) daemon running"
else
  # Check if C binary is running instead
  if pgrep -f "driver/build/mic-forward" >/dev/null 2>&1; then
    warn "mic-forward C binary running (should be Python for Tauri integration)"
    info "The C binary doesn't write mic-levels.json or read mic-settings.json"
  else
    warn "mic-forward daemon NOT running"
  fi
fi

# Check for stale C mic-forward (should NOT be running alongside Python)
if pgrep -f "mic-forward.py" >/dev/null 2>&1 && pgrep -f "driver/build/mic-forward" >/dev/null 2>&1; then
  fail "BOTH Python and C mic-forward running! They'll double-write to CaptureMic 2ch"
  info "Fix: kill the C binary: kill \$(pgrep -f 'driver/build/mic-forward')"
fi
echo ""

# ─── 4. Data files fresh ─────────────────────────────────────────────────
echo "Data Files:"

check_freshness() {
  local name="$1" path="$2" max_age="$3"
  if [[ ! -f "$path" ]]; then
    warn "$name not found at $path"
    return
  fi
  local age
  age=$(( $(date +%s) - $(stat -f %m "$path") ))
  if (( age <= max_age )); then
    ok "$name fresh (${age}s old)"
  else
    warn "$name stale (${age}s old, max ${max_age}s)"
  fi
}

check_freshness "mic-levels.json" "$ROOT_DIR/data/mic-levels.json" 5
check_freshness "mic-settings.json" "$ROOT_DIR/data/mic-settings.json" 86400
check_freshness "speaker-settings.json" "$ROOT_DIR/data/speaker-settings.json" 86400
echo ""

# ─── 5. API health ───────────────────────────────────────────────────────
echo "API Health:"

check_api() {
  local name="$1" url="$2"
  local status
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null || echo "000")
  if [[ "$status" == "200" ]]; then
    ok "$name ($url)"
  else
    warn "$name returned HTTP $status ($url)"
  fi
}

check_api "audio-assist health" "http://127.0.0.1:8787/health"
check_api "volume-control devices" "http://127.0.0.1:8788/api/devices"
check_api "volume-control mics" "http://127.0.0.1:8788/api/mics"
check_api "discord-bot health" "http://127.0.0.1:8796/health"
echo ""

# ─── 6. Audio-forward routing ────────────────────────────────────────────
echo "Audio Forward Routing:"
if [[ -f "$ROOT_DIR/data/logs/audio-forward.log" ]]; then
  # Check last few active targets
  active=$(grep -o '\[.*\] active' "$ROOT_DIR/data/logs/audio-forward.log" | tail -10 | sort -u)
  if [[ -n "$active" ]]; then
    while IFS= read -r line; do
      ok "Routing: $line"
    done <<< "$active"
  else
    warn "No active routing targets found in audio-forward.log"
  fi
else
  warn "audio-forward.log not found"
fi
echo ""

# ─── 7. Capture sources ──────────────────────────────────────────────────
echo "Capture Sources (audio-assist):"
readiness=$(curl -s --max-time 2 "http://127.0.0.1:8787/v1/capture/readiness" 2>/dev/null || echo '{}')
if [[ "$readiness" != "{}" ]]; then
  status=$(echo "$readiness" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null)
  sources=$(echo "$readiness" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for s in d.get('signals',{}).get('running_sources', []):
    print(s)
" 2>/dev/null)
  if [[ "$status" == "ready" ]]; then
    ok "Capture status: ready"
  elif [[ "$status" == "degraded" ]]; then
    warn "Capture status: degraded"
  else
    fail "Capture status: $status"
  fi
  if [[ -n "$sources" ]]; then
    while IFS= read -r src; do
      ok "Source running: $src"
    done <<< "$sources"
  fi
else
  warn "Could not reach audio-assist readiness endpoint"
fi
echo ""

# ─── Summary ──────────────────────────────────────────────────────────────
echo "=== Summary ==="
if (( errors > 0 )); then
  echo -e "${RED}$errors error(s)${RST}, ${YEL}$warnings warning(s)${RST}"
  echo "See docs/audio-routing-architecture.md for the expected architecture."
  exit 2
elif (( warnings > 0 )); then
  echo -e "${GRN}0 errors${RST}, ${YEL}$warnings warning(s)${RST}"
  exit 1
else
  echo -e "${GRN}All checks passed${RST}"
  exit 0
fi
