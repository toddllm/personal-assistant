#!/usr/bin/env python3
"""Intercept macOS media volume keys and forward to volume-control API.

macOS volume keys are NX_SYSDEFINED events (not regular key events), so
Carbon RegisterEventHotKey / Tauri global-shortcut can't catch them.
This daemon uses a Quartz CGEventTap to intercept them system-wide.

Uses kCGEventTapOptionListenOnly (passive) so no Accessibility permission
is required — events still pass through to macOS normally.
"""

import json
import signal
import sys
import urllib.request

import Quartz

VOLUME_STEP_URL = "http://127.0.0.1:8788/api/volume-step"
STEP_SIZE = 2

# NX_SYSDEFINED media key subcodes
NX_KEYTYPE_SOUND_UP = 0
NX_KEYTYPE_SOUND_DOWN = 1
NX_KEYTYPE_MUTE = 7

# CGEventMask for NX_SYSDEFINED (event type 14)
NX_SYSDEFINED = 14
EVENT_MASK = (1 << NX_SYSDEFINED)


def _post_step(step: int) -> None:
    try:
        req = urllib.request.Request(
            VOLUME_STEP_URL,
            data=json.dumps({"step": step}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=0.5)
    except Exception as e:
        print(f"volume-keys: API error: {e}", flush=True)


def _callback(proxy, event_type, event, refcon):
    # Convert CG event to NSEvent to read media key data
    ns_event = Quartz.NSEvent.eventWithCGEvent_(event)
    if ns_event is None:
        return event

    if ns_event.subtype() != 8:  # NX_SUBTYPE_AUX_CONTROL_BUTTONS
        return event

    data = ns_event.data1()
    key_code = (data & 0xFFFF0000) >> 16
    key_flags = (data & 0x0000FF00) >> 8
    key_down = (key_flags & 0x0A) == 0x0A  # key-down event

    if not key_down:
        return event

    if key_code == NX_KEYTYPE_SOUND_UP:
        print(f"volume-keys: volume up (+{STEP_SIZE})", flush=True)
        _post_step(STEP_SIZE)
    elif key_code == NX_KEYTYPE_SOUND_DOWN:
        print(f"volume-keys: volume down (-{STEP_SIZE})", flush=True)
        _post_step(-STEP_SIZE)
    elif key_code == NX_KEYTYPE_MUTE:
        print("volume-keys: mute (ignored)", flush=True)

    return event


def main():
    # Unbuffer stdout for logging
    sys.stdout.reconfigure(line_buffering=True)

    print(f"volume-keys: intercepting media keys, step={STEP_SIZE}%")
    print(f"volume-keys: posting to {VOLUME_STEP_URL}")

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        EVENT_MASK,
        _callback,
        None,
    )

    if tap is None:
        print("volume-keys: ERROR — failed to create event tap", file=sys.stderr)
        print("volume-keys: check System Settings > Privacy > Input Monitoring", file=sys.stderr)
        sys.exit(1)

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = Quartz.CFRunLoopGetCurrent()
    Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopDefaultMode)
    Quartz.CGEventTapEnable(tap, True)

    print("volume-keys: listening (Ctrl+C to stop)")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    Quartz.CFRunLoopRun()


if __name__ == "__main__":
    main()
