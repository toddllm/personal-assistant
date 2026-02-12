"""Detect active meetings on macOS via process and window inspection."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MeetingInfo:
    """Snapshot of a detected active meeting."""

    active: bool = False
    provider: str | None = None
    title: str | None = None
    started_at: datetime | None = None
    confidence: float = 0.0
    details: dict[str, str] = field(default_factory=dict)


def _run_osascript(script: str, timeout_seconds: float = 3.0) -> str:
    """Run an AppleScript and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.debug("osascript timed out after %.1fs", timeout_seconds)
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _check_zoom() -> MeetingInfo | None:
    """Detect if Zoom is in an active meeting."""
    # Check if Zoom process is running
    script = '''
    tell application "System Events"
        set zoomRunning to (name of processes) contains "zoom.us"
    end tell
    return zoomRunning as text
    '''
    running = _run_osascript(script)
    if running != "true":
        return None

    # Check for meeting window - Zoom shows "Zoom Meeting" or specific meeting titles
    script = '''
    tell application "System Events"
        tell process "zoom.us"
            set windowNames to name of every window
        end tell
    end tell
    return windowNames as text
    '''
    windows = _run_osascript(script)
    if not windows:
        return None

    # Zoom meeting windows contain "Zoom Meeting" or a specific meeting title
    # Non-meeting windows are typically "Zoom" (home), "Zoom Workplace", "Chat", "Settings"
    non_meeting_windows = {
        "zoom", "zoom workplace", "zoom workplace - free", "chat", "settings",
        "preferences", "contacts", "", "zoom cloud meetings",
    }

    meeting_title = None
    for win_name in windows.split(", "):
        cleaned = win_name.strip()
        if cleaned.lower() not in non_meeting_windows and cleaned:
            meeting_title = cleaned
            break

    if meeting_title is None:
        return None

    # Check if audio is active in the meeting via menu bar state
    script = '''
    tell application "System Events"
        tell process "zoom.us"
            try
                set menuItems to name of every menu item of menu 1 of menu bar item "Meeting" of menu bar 1
                return menuItems as text
            on error
                return ""
            end try
        end tell
    end tell
    '''
    menu_items = _run_osascript(script)
    has_meeting_menu = bool(menu_items)

    confidence = 0.8 if has_meeting_menu else 0.6

    return MeetingInfo(
        active=True,
        provider="zoom",
        title=meeting_title,
        confidence=confidence,
        details={"windows": windows},
    )


def _check_teams() -> MeetingInfo | None:
    """Detect if Microsoft Teams is in an active call."""
    script = '''
    tell application "System Events"
        set teamsRunning to (name of processes) contains "Microsoft Teams"
        if not teamsRunning then
            set teamsRunning to (name of processes) contains "Microsoft Teams (work or school)"
        end if
        if not teamsRunning then
            set teamsRunning to (name of processes) contains "Microsoft Teams classic"
        end if
    end tell
    return teamsRunning as text
    '''
    running = _run_osascript(script)
    if running != "true":
        return None

    # Teams shows specific windows during calls
    for process_name in [
        "Microsoft Teams",
        "Microsoft Teams (work or school)",
        "Microsoft Teams classic",
    ]:
        script = f'''
        tell application "System Events"
            try
                tell process "{process_name}"
                    set windowNames to name of every window
                end tell
                return windowNames as text
            on error
                return ""
            end try
        end tell
        '''
        windows = _run_osascript(script)
        if not windows:
            continue

        # Teams in-call windows typically contain the meeting title or "Microsoft Teams"
        # with specific UI elements. Check for call indicator.
        for win_name in windows.split(", "):
            cleaned = win_name.strip()
            lowered = cleaned.lower()
            if any(
                keyword in lowered
                for keyword in ["call", "meeting", "call with"]
            ):
                return MeetingInfo(
                    active=True,
                    provider="teams",
                    title=cleaned,
                    confidence=0.7,
                    details={"windows": windows, "process": process_name},
                )

    return None


def _check_browser_meet() -> MeetingInfo | None:
    """Detect if Google Meet or similar is active in a browser tab."""
    # Check Chrome/Arc/Edge for Google Meet tabs with active audio
    browsers = [
        ("Google Chrome", "Google Chrome"),
        ("Arc", "Arc"),
        ("Microsoft Edge", "Microsoft Edge"),
        ("Brave Browser", "Brave Browser"),
    ]

    for app_name, process_name in browsers:
        script = f'''
        tell application "System Events"
            set isRunning to (name of processes) contains "{process_name}"
        end tell
        if not isRunning then return ""

        tell application "{app_name}"
            try
                set tabTitles to ""
                repeat with w in windows
                    repeat with t in tabs of w
                        set tabURL to URL of t
                        set tabTitle to title of t
                        if tabURL contains "meet.google.com" or tabURL contains "teams.microsoft.com/v2" then
                            set tabTitles to tabTitles & tabTitle & "|||"
                        end if
                    end repeat
                end repeat
                return tabTitles
            on error
                return ""
            end try
        end tell
        '''
        result = _run_osascript(script, timeout_seconds=4.0)
        if not result:
            continue

        tabs = [t.strip() for t in result.split("|||") if t.strip()]
        if not tabs:
            continue

        # Filter out non-meeting pages (calendar, landing, sign-in)
        non_meeting_indicators = [
            "google meet", "join a meeting", "sign in", "calendar",
            "sign up", "get started", "start a meeting", "schedule",
            "home -", "chat -", "activity -", "teams -",
        ]

        active_tabs = []
        for tab in tabs:
            lowered = tab.lower()
            if any(skip in lowered for skip in non_meeting_indicators):
                continue
            active_tabs.append(tab)

        if not active_tabs:
            continue

        title = active_tabs[0]

        return MeetingInfo(
            active=True,
            provider=f"browser-meet ({app_name.lower()})",
            title=title,
            confidence=0.65,
            details={"browser": app_name, "tabs": ", ".join(active_tabs)},
        )

    return None


def _check_webex() -> MeetingInfo | None:
    """Detect if Webex is in an active meeting."""
    script = '''
    tell application "System Events"
        set webexRunning to false
        set procNames to name of every process
        repeat with procName in procNames
            if procName contains "Webex" then
                set webexRunning to true
                exit repeat
            end if
        end repeat
    end tell
    return webexRunning as text
    '''
    running = _run_osascript(script)
    if running != "true":
        return None

    script = '''
    tell application "System Events"
        set procNames to name of every process
        repeat with procName in procNames
            if procName contains "Webex" then
                try
                    tell process (procName as text)
                        set windowNames to name of every window
                    end tell
                    return windowNames as text
                on error
                    return ""
                end try
            end if
        end repeat
    end tell
    return ""
    '''
    windows = _run_osascript(script)
    if not windows:
        return None

    for win_name in windows.split(", "):
        cleaned = win_name.strip()
        if cleaned and "webex" not in cleaned.lower():
            return MeetingInfo(
                active=True,
                provider="webex",
                title=cleaned,
                confidence=0.6,
                details={"windows": windows},
            )

    return None


class MeetingDetector:
    """Polls macOS system state to detect active meetings."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._last_state: MeetingInfo = MeetingInfo()
        self._last_checked_at: datetime | None = None
        self._meeting_started_at: datetime | None = None
        self._consecutive_active = 0
        self._consecutive_inactive = 0

    def detect(self) -> MeetingInfo:
        """Check all providers and return the highest-confidence active meeting."""
        now = datetime.now(tz=UTC)
        checks = [_check_zoom, _check_teams, _check_browser_meet, _check_webex]

        best: MeetingInfo | None = None
        for check_fn in checks:
            try:
                result = check_fn()
                if result is not None and result.active:
                    if best is None or result.confidence > best.confidence:
                        best = result
            except Exception:  # noqa: BLE001
                logger.debug("Meeting check %s failed.", check_fn.__name__, exc_info=True)

        with self._lock:
            self._last_checked_at = now
            if best is not None and best.active:
                self._consecutive_active += 1
                self._consecutive_inactive = 0
                if self._meeting_started_at is None:
                    self._meeting_started_at = now
                best.started_at = self._meeting_started_at
                self._last_state = best
                return best
            else:
                self._consecutive_inactive += 1
                self._consecutive_active = 0
                if self._consecutive_inactive >= 2:
                    self._meeting_started_at = None
                no_meeting = MeetingInfo(active=False)
                self._last_state = no_meeting
                return no_meeting

    @property
    def last_state(self) -> MeetingInfo:
        with self._lock:
            return self._last_state

    @property
    def last_checked_at(self) -> datetime | None:
        with self._lock:
            return self._last_checked_at

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "active": self._last_state.active,
                "provider": self._last_state.provider,
                "title": self._last_state.title,
                "started_at": self._last_state.started_at,
                "confidence": self._last_state.confidence,
                "last_checked_at": self._last_checked_at,
                "consecutive_active": self._consecutive_active,
                "consecutive_inactive": self._consecutive_inactive,
            }
