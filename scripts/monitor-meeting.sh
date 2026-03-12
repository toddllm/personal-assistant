#!/usr/bin/env bash
# Monitor audio devices, meeting apps, and Krisp during a live meeting.
# Usage: bash scripts/monitor-meeting.sh
# Output goes to data/logs/meeting-monitor.log

LOG_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/meeting-monitor.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOGFILE"
}

check_state() {
    log "=== SNAPSHOT ==="

    # Audio routing
    local output_dev
    output_dev=$(SwitchAudioSource -c 2>/dev/null || echo "unknown")
    local input_dev
    input_dev=$(SwitchAudioSource -c -t input 2>/dev/null || echo "unknown")
    log "Audio Output: $output_dev"
    log "Audio Input:  $input_dev"

    # Krisp process
    local krisp_pid
    krisp_pid=$(pgrep -f "krisp.app/Contents/MacOS/krisp$" 2>/dev/null | head -1)
    if [ -n "$krisp_pid" ]; then
        log "Krisp: RUNNING (pid $krisp_pid)"
    else
        log "Krisp: not running"
    fi

    # Meeting apps
    for app in "zoom.us" "Microsoft Teams" "Google Chrome" "Arc" "Brave Browser"; do
        if pgrep -x "$app" >/dev/null 2>&1 || pgrep -f "$app" >/dev/null 2>&1; then
            log "App: $app is running"
        fi
    done

    # Check for meeting windows via osascript (quick check)
    local zoom_windows
    zoom_windows=$(osascript -e '
        tell application "System Events"
            if (name of processes) contains "zoom.us" then
                tell process "zoom.us"
                    return name of every window
                end tell
            end if
        end tell
        return ""
    ' 2>/dev/null)
    if [ -n "$zoom_windows" ]; then
        log "Zoom windows: $zoom_windows"
    fi

    # Check for meeting-related browser tabs
    local meet_tabs
    meet_tabs=$(osascript -e '
        tell application "System Events"
            set isRunning to (name of processes) contains "Google Chrome"
        end tell
        if not isRunning then return ""
        tell application "Google Chrome"
            try
                set result to ""
                repeat with w in windows
                    repeat with t in tabs of w
                        set tabURL to URL of t
                        if tabURL contains "meet.google.com" or tabURL contains "teams.microsoft.com" or tabURL contains "zoom.us" then
                            set result to result & (title of t) & " | "
                        end if
                    end repeat
                end repeat
                return result
            on error
                return ""
            end try
        end tell
    ' 2>/dev/null)
    if [ -n "$meet_tabs" ]; then
        log "Browser meeting tabs: $meet_tabs"
    fi

    log "---"
}

log "Meeting monitor started. Polling every 15s. Ctrl+C to stop."
log "Krisp devices present: krisp microphone, krisp speaker"

while true; do
    check_state
    sleep 15
done
