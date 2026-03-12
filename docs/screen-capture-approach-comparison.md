# Screen Capture Approach Comparison for Issue 2.1

**Date:** 2026-02-13
**System:** macOS 15.7.2 (Sequoia), Apple Silicon, Python 3.12.4
**Purpose:** Select the best approach for periodic screenshot capture (every 10-30 seconds) during meetings, running in the background as part of the audio-assist Python service.

---

## Table of Contents

1. [Executive Summary and Recommendation](#executive-summary-and-recommendation)
2. [Benchmark Results](#benchmark-results)
3. [Approach Evaluations](#approach-evaluations)
4. [macOS Screen Recording Permission Model](#macos-screen-recording-permission-model)
5. [WebP Compression Analysis](#webp-compression-analysis)
6. [Zoom Window Detection](#zoom-window-detection)
7. [Implementation Plan](#implementation-plan)

---

## Executive Summary and Recommendation

**Recommended approach: `CGWindowListCreateImage` (CoreGraphics) via PyObjC, with `mss` as a fallback for full-screen capture.**

### Rationale

| Criterion | CGWindowListCreateImage | mss | screencapture CLI | PyAutoGUI | ScreenCaptureKit |
|---|---|---|---|---|---|
| Full-screen speed | 46ms | 40ms | 180ms | 247ms | N/A (async) |
| Window-specific speed | 26ms | N/A | 120ms | N/A | TBD |
| Window-specific capture | Yes (by window ID) | No | Yes (-l flag) | No | Yes |
| Retina resolution | Yes (2x native) | No (1x logical) | Yes (2x native) | Yes (2x native) | Yes |
| Python integration | Excellent (PyObjC) | Excellent (pure Python) | Subprocess only | OK | Complex (async) |
| Background operation | Yes | Yes | Yes | Yes | Yes |
| Already installed | Yes (PyObjC 10.3.1) | Yes (mss 10.1.0) | Yes (built-in) | Yes | Yes (PyObjC) |
| Deprecation risk | Deprecated macOS 14+ | None | None | None | None (replacement) |
| CPU duty cycle (15s) | ~1.0% | ~0.4% | ~1.2% | ~1.6% | TBD |

### Why CGWindowListCreateImage wins

1. **Window-specific capture at 26ms** -- the fastest option for capturing only the Zoom window, which is the primary use case.
2. **Retina resolution** -- captures at 2x native (3456x2234), providing sharp text for later OCR/analysis. `mss` only captures at logical resolution (1728x1117).
3. **Already available** -- PyObjC 10.3.1 is already installed in the project environment with the Quartz framework.
4. **Direct PIL integration** -- CGImage data converts directly to a Pillow Image without subprocess overhead.
5. **Window enumeration** -- The same API (`CGWindowListCopyWindowInfo`) provides window discovery, enabling Zoom window detection by owner name and window title.

### Why not the others

- **`mss`**: Fastest for full-screen but cannot capture specific windows and only returns 1x logical resolution. Use as a fallback if CGWindowListCreateImage ever returns None (permission denied).
- **`screencapture` CLI**: 120ms for window capture (4.6x slower than CG). Subprocess overhead, produces files instead of in-memory data, no direct WebP output.
- **`PyAutoGUI`**: Slowest at 247ms, no window-specific capture, primarily a GUI automation tool.
- **`ScreenCaptureKit`**: The modern replacement for CGWindowListCreateImage, but its async callback-based API is extremely difficult to use from Python via PyObjC. The `SCScreenshotManager` class methods require `NSRunLoop` integration and completion handlers. Worth revisiting if/when CGWindowListCreateImage stops working entirely.

### Deprecation mitigation strategy

`CGWindowListCreateImage` was deprecated in macOS 14 and obsoleted in macOS 15 headers, but **it still works on macOS 15.7.2** (confirmed by benchmarks). Apple has not removed the runtime implementation. The mitigation plan:

1. **Primary**: Use `CGWindowListCreateImage` -- it works today and is the best option.
2. **Fallback 1**: If CG returns None, fall back to `mss` for full-screen capture.
3. **Fallback 2**: If Apple removes CG in a future macOS update, migrate to `ScreenCaptureKit` (SCScreenshotManager) or `screencapture -l`.
4. **Architecture**: Abstract the capture backend behind a `ScreenCapturer` protocol so swapping implementations is trivial.

---

## Benchmark Results

All benchmarks measured on macOS 15.7.2 (Sequoia), Apple Silicon, with a 3456x2234 Retina display.

### Raw Capture Speed (averaged over 5-10 iterations)

| Method | Full Screen | Window-Specific | Notes |
|---|---|---|---|
| `CGWindowListCreateImage` | 46ms | 26ms | Retina (3456x2234) |
| `mss` | 40ms | N/A | Logical (1728x1117) |
| `screencapture` CLI | 180ms | 120ms | Writes to file |
| `PyAutoGUI` | 247ms | N/A | Retina (3456x2234) |

### Full Pipeline: Capture + Resize to 50% + WebP Compression

| Pipeline | Avg Time | Output Size | CPU Duty (15s interval) |
|---|---|---|---|
| CG + PIL resize + WebP q50 m0 | 156ms | 110 KB | 1.04% |
| mss + PIL resize + WebP q50 m0 | 65ms | 28 KB | 0.43% |
| screencapture -l (PNG) | 122ms | 139 KB | 0.81% |
| screencapture -l (JPEG) | 103ms | 214 KB | 0.69% |

**Note:** The `mss` pipeline output is only 28 KB because it captures at 1x logical resolution (1728x1117), then resizes to 864x558. The CG pipeline captures at 2x Retina (3456x2234), resizes to 1728x1117, producing a significantly sharper image.

### Memory Usage

- Peak memory during CG capture + WebP pipeline: **29 MB**
- This is negligible relative to the Whisper transcription model (~300-800 MB) already running in the service.

---

## Approach Evaluations

### 1. `screencapture` CLI (macOS built-in)

**How it works:** Shell out to `/usr/sbin/screencapture` via `subprocess.run()`.

```python
subprocess.run(["screencapture", "-x", "-l", str(window_id), "-t", "png", output_path])
```

**Pros:**
- Always available on macOS, no dependencies
- Supports window-specific capture via `-l <windowid>` flag
- Supports multiple output formats: png, jpg, tiff, pdf
- Inherits screen recording permission from the calling process (Terminal/Python)
- Supports region capture via `-R x,y,w,h`
- Supports display selection via `-D <display_number>`

**Cons:**
- Subprocess overhead: 120-180ms per capture
- Writes to file only -- no in-memory capture, requires disk I/O
- No WebP output format (must convert separately)
- Cannot capture without writing to disk (or using clipboard with `-c`, which is disruptive)
- Slower than native APIs by 2-4x

**Available flags of interest:**
- `-x` -- silent (no screenshot sound)
- `-l <windowid>` -- capture specific window
- `-C` -- include cursor
- `-o` -- exclude window shadow
- `-R x,y,w,h` -- capture screen region
- `-D <display>` -- specific display (1=main, 2=secondary)
- `-t <format>` -- output format (png, jpg, tiff, pdf)
- `-m` -- main monitor only

**Verdict:** Reliable fallback but too slow for primary use. Subprocess overhead and file I/O make it 3-5x slower than native APIs.

### 2. `CGWindowListCreateImage` (CoreGraphics via PyObjC)

**How it works:** Call CoreGraphics C functions directly through PyObjC bindings.

```python
import Quartz

# Full screen
image = Quartz.CGWindowListCreateImage(
    Quartz.CGRectInfinite,
    Quartz.kCGWindowListOptionAll,
    Quartz.kCGNullWindowID,
    Quartz.kCGWindowImageDefault,
)

# Specific window
image = Quartz.CGWindowListCreateImage(
    Quartz.CGRectNull,
    Quartz.kCGWindowListOptionIncludingWindow,
    window_id,
    Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution,
)
```

**Pros:**
- Fastest window-specific capture: 26ms
- Full Retina resolution (3456x2234 on this display)
- Window-specific capture by window ID, with window enumeration via `CGWindowListCopyWindowInfo`
- Returns CGImage in memory -- no disk I/O required
- Direct conversion to Pillow Image for processing
- PyObjC 10.3.1 already installed in the project
- Can capture windows in the background (not frontmost)

**Cons:**
- Deprecated in macOS 14 headers, obsoleted in macOS 15 headers
- Still works at runtime on macOS 15.7.2 (confirmed)
- Requires Screen Recording permission via TCC
- macOS-only (not cross-platform)
- CGImage-to-PIL conversion adds ~20ms

**CGImage to PIL conversion:**
```python
from PIL import Image

width = Quartz.CGImageGetWidth(cg_image)
height = Quartz.CGImageGetHeight(cg_image)
bytes_per_row = Quartz.CGImageGetBytesPerRow(cg_image)
provider = Quartz.CGImageGetDataProvider(cg_image)
data = Quartz.CGDataProviderCopyData(provider)

pil_image = Image.frombytes("RGBA", (width, height), bytes(data), "raw", "BGRA", bytes_per_row)
```

**Window enumeration for Zoom detection:**
```python
windows = Quartz.CGWindowListCopyWindowInfo(
    Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
    Quartz.kCGNullWindowID,
)
for w in windows:
    if w.get("kCGWindowOwnerName") == "zoom.us":
        wid = w["kCGWindowNumber"]
        name = w.get("kCGWindowName", "")
        bounds = w["kCGWindowBounds"]
        # Filter for main meeting window by size and name
```

**Verdict:** Best overall choice. Fastest for the primary use case (window-specific capture), full Retina resolution, excellent Python integration, already installed.

### 3. `mss` Python Library

**How it works:** Pure Python library using ctypes to call native screen capture APIs.

```python
import mss

with mss.mss() as sct:
    image = sct.grab(sct.monitors[0])  # Full screen
    # Returns an mss.ScreenShot object with .rgb, .bgra properties
```

**Pros:**
- Fastest full-screen capture: 40ms
- Pure Python, no compilation needed
- Cross-platform (macOS, Windows, Linux)
- Thread-safe
- No dependencies
- Lightweight (24 KB wheel)
- Excellent for full-screen capture

**Cons:**
- Cannot capture specific windows -- full screen only
- Returns logical resolution (1728x1117) on Retina displays, not native 2x resolution
- No window enumeration or discovery
- Region-based capture requires knowing exact coordinates
- Would need to combine with CGWindowListCopyWindowInfo for window bounds, then crop

**Multi-monitor support:**
```python
with mss.mss() as sct:
    # sct.monitors[0] = combined virtual screen
    # sct.monitors[1] = first display
    # sct.monitors[2] = second display (if present)
    for i, monitor in enumerate(sct.monitors):
        print(f"Monitor {i}: {monitor}")
```

**Verdict:** Excellent for full-screen fallback. The 1x resolution limitation and lack of window-specific capture make it unsuitable as the primary approach for meeting capture.

### 4. `PyAutoGUI`

**How it works:** High-level GUI automation library that wraps `screencapture` on macOS.

```python
import pyautogui
screenshot = pyautogui.screenshot()  # Returns PIL Image
screenshot = pyautogui.screenshot(region=(x, y, width, height))  # Region capture
```

**Pros:**
- Returns PIL Image directly
- Simple API
- Cross-platform
- Region capture support

**Cons:**
- Slowest option: 247ms per capture
- Internally calls `screencapture` on macOS -- just a wrapper with extra overhead
- No window-specific capture
- Primarily designed for GUI automation, not screenshot capture
- Heavy dependency for a single use case
- Cannot capture background windows

**Verdict:** Not suitable. Slowest option, no window-specific capture, unnecessary overhead wrapping `screencapture`.

### 5. `Pillow` + `subprocess` (Hybrid)

**How it works:** Use `screencapture` CLI for capture, then Pillow for processing/compression.

```python
import subprocess
from PIL import Image

subprocess.run(["screencapture", "-x", "-l", str(wid), "/tmp/capture.png"])
img = Image.open("/tmp/capture.png")
img.save("/tmp/capture.webp", format="WEBP", quality=50)
```

**Pros:**
- Reliable -- uses well-tested tools
- Full format support for output (WebP, JPEG, PNG)
- Window-specific capture via screencapture -l

**Cons:**
- Double disk I/O (write PNG, read PNG, write WebP)
- Slowest pipeline due to subprocess + file I/O + conversion
- ~200-250ms total for capture + convert
- Temporary file management required

**Verdict:** Functional but inefficient. The CG + PIL approach achieves the same result without any disk I/O.

### 6. (Bonus) ScreenCaptureKit via PyObjC

**How it works:** Apple's modern replacement for CGWindowListCreateImage, introduced in macOS 12.3.

```python
import ScreenCaptureKit

# SCScreenshotManager.captureSampleBufferWithFilter_configuration_completionHandler_()
# SCShareableContent.getShareableContentWithCompletionHandler_()
```

**Pros:**
- Apple's recommended replacement API (not deprecated)
- Supports window, display, and application-level capture
- Higher quality capture options
- Future-proof

**Cons:**
- Async callback-based API -- extremely difficult to use from Python
- Requires `NSRunLoop` or `CFRunLoop` integration for completion handlers
- `SCScreenshotManager` class methods are not exposed as simple synchronous calls via PyObjC
- Limited Python documentation and examples
- Would require writing an Objective-C bridge or using `asyncio` with PyObjC
- The `SCContentFilter` init methods are available:
  - `initWithDesktopIndependentWindow_` -- single window
  - `initWithDisplay_excludingWindows_` -- display minus windows
  - `initWithDisplay_includingWindows_` -- display with specific windows

**Verdict:** The correct long-term solution but impractical today for a Python service. The async API requires significant bridging work. Worth revisiting when PyObjC provides synchronous wrapper support or when CGWindowListCreateImage is actually removed from the runtime.

---

## macOS Screen Recording Permission Model

### How TCC Works

macOS uses **Transparency, Consent, and Control (TCC)** to manage privacy permissions. Screen recording falls under the `kTCCServiceScreenCapture` service.

**Permission flow:**
1. An app attempts to capture the screen (via CGWindowListCreateImage, ScreenCaptureKit, screencapture, etc.)
2. macOS checks the TCC database for that app's bundle ID or binary path
3. If no permission exists, the system shows a consent dialog
4. The user must manually enable Screen Recording in **System Settings > Privacy & Security > Screen Recording**
5. The app may need to be restarted after granting permission

**TCC database locations:**
- User-level: `~/Library/Application Support/com.apple.TCC/TCC.db` (not readable without Full Disk Access)
- System-level: `/Library/Application Support/com.apple.TCC/TCC.db` (requires root)

### macOS Sequoia (15.x) Changes

Apple made significant changes to screen recording permissions in macOS Sequoia:

1. **Monthly re-authorization prompts**: Apps with screen recording permission now trigger a monthly reminder asking the user to confirm the permission. This was changed from weekly (in early betas) to monthly after developer backlash.

2. **ScreenCaptureApprovals.plist**: The system tracks approval timestamps in `~/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist`. This file controls when the next re-authorization prompt appears.

3. **Persistent Content Capture entitlement**: Apple offers a special entitlement (`com.apple.developer.persistent-content-capture`) that exempts apps from the monthly prompt. This is intended for VNC apps and requires applying through Apple's developer program.

4. **MDM override**: macOS 15.1 introduced MDM configuration profiles that can suppress screen recording prompts across managed devices.

### Permission Strategy for This Project

Since the audio-assist service runs as a Python process (via `python3` or a shell script):

1. **The parent process needs Screen Recording permission.** If running from Terminal.app, iTerm2, or VS Code terminal, that application needs Screen Recording enabled.
2. **Grant once, monthly re-auth.** The user will see a monthly prompt to re-authorize. This is unavoidable without the Persistent Content Capture entitlement.
3. **Graceful degradation.** If permission is denied, `CGWindowListCreateImage` returns None. The code must handle this gracefully:

```python
image = Quartz.CGWindowListCreateImage(...)
if image is None:
    logger.warning("Screen capture permission denied. Falling back to no-capture mode.")
    return None
```

4. **First-run prompt.** On first use, the system will show a dialog asking to grant Screen Recording permission to the terminal app. This is a one-time manual step per terminal application.

### How to Request Permission Programmatically

There is no API to programmatically grant Screen Recording permission. However, you can:

1. **Detect if permission is granted** by attempting a capture and checking the result:
```python
def has_screen_recording_permission() -> bool:
    """Check if screen recording permission is granted."""
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite,
        Quartz.kCGWindowListOptionAll,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    return image is not None
```

2. **Open System Settings** to the correct pane:
```python
subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
```

3. **Show a user-friendly message** explaining why permission is needed and how to grant it.

---

## WebP Compression Analysis

### Benchmark Results (3456x2234 Retina full-screen capture)

| Format | Quality/Settings | File Size | Encode Time | Notes |
|---|---|---|---|---|
| PNG (no optimize) | -- | 996 KB | 537ms | Lossless, baseline |
| JPEG | q=75 | 442 KB | 19ms | Fastest encode |
| WebP | q=30, method=0 | 204 KB | 183ms | High compression, fast |
| WebP | q=50, method=0 | 224 KB | 88ms | Good balance |
| WebP | q=70, method=0 | 230 KB | 88ms | Diminishing returns |
| WebP | q=85, method=0 | 279 KB | 92ms | High quality |
| WebP | q=95, method=0 | 385 KB | 133ms | Near-lossless |
| WebP | q=50, method=4 | 164 KB | 262ms | Better compression, slower |
| **WebP 50% scale** | **q=50, method=0** | **95 KB** | **26ms** | **Recommended** |

### Recommended Settings

For meeting screenshots captured every 15 seconds:

```python
# Resize to 50% of Retina resolution (1728x1117 -> matches logical resolution)
resized = pil_image.resize((width // 2, height // 2), Image.LANCZOS)

# WebP with quality=50, method=0 (fastest encoder)
buffer = io.BytesIO()
resized.save(buffer, format="WEBP", quality=50, method=0)
# Result: ~95-110 KB per frame, ~26ms encode time
```

**Why these settings:**
- **50% scale**: Retina 2x resolution is unnecessary for meeting content review. 1x logical resolution (1728x1117) is more than sufficient.
- **quality=50**: Screenshots of meeting windows compress extremely well because they contain mostly flat UI elements, text, and limited photographic content. Quality 50 preserves all readable text.
- **method=0**: Fastest WebP encoder. method=4 saves ~30% more space but takes 10x longer (262ms vs 26ms).
- **~95 KB per frame**: At 15-second intervals for a 1-hour meeting, that is 240 frames = ~22 MB total. Very manageable.

### Storage Estimates

| Interval | Frames/Hour | Size/Hour | Size/8hr Day |
|---|---|---|---|
| 10 seconds | 360 | 33 MB | 264 MB |
| 15 seconds | 240 | 22 MB | 176 MB |
| 30 seconds | 120 | 11 MB | 88 MB |

---

## Zoom Window Detection

### Current Meeting Detection

The existing `MeetingDetector` class in `src/audio_assist/meeting_detector.py` already detects active Zoom meetings via AppleScript, checking window names and menu bar state. This provides the meeting title and active state.

### Window ID Discovery for Screen Capture

For screen capture, we need the actual window ID (CGWindowID). The approach:

```python
import Quartz

def find_zoom_meeting_window() -> int | None:
    """Find the Zoom meeting window ID for screen capture."""
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )

    NON_MEETING_NAMES = {
        "zoom workplace", "zoom", "settings", "login",
        "zoom client healthcheck", "",
    }

    best_window = None
    best_area = 0

    for w in windows:
        owner = w.get("kCGWindowOwnerName", "")
        if owner != "zoom.us":
            continue

        name = w.get("kCGWindowName", "")
        layer = w.get("kCGWindowLayer", -1)
        bounds = w.get("kCGWindowBounds", {})
        width = bounds.get("Width", 0)
        height = bounds.get("Height", 0)

        # Skip non-meeting windows
        if name.lower() in NON_MEETING_NAMES:
            continue
        if layer != 0:  # Only main layer windows
            continue
        if width < 400 or height < 300:  # Skip small utility windows
            continue

        area = width * height
        if area > best_area:
            best_area = area
            best_window = w.get("kCGWindowNumber")

    return best_window
```

### Observed Zoom Window Hierarchy (from benchmark data)

On this system, Zoom (PID 497, bundle `us.zoom.xos`) exposes these windows:

| Window Name | Size | Layer | Purpose |
|---|---|---|---|
| `Zoom Workplace` | 1728x1079 | 0 | Home screen (non-meeting) |
| `Zoom` | 1728x1079 | 0 | Meeting window (when in meeting) |
| `zoom floating video window` | 244x139 | 26 | Picture-in-picture |
| `Settings` | 800x660 | 3 | Settings dialog |
| `Login` | 1224x720 | 0 | Login window |
| `Zoom Client Healthcheck` | 880x600 | 0 | Health check |
| Various unnamed | Various | Various | Toolbars, status bars |

**Key insight:** During an active meeting, the main meeting window is named after the meeting title (e.g., "AI Exchange Fridays!") or generically "Zoom Meeting". The `Zoom Workplace` home screen is always present as a separate window.

### Bundle ID Detection via NSWorkspace

```python
import AppKit

def is_zoom_running() -> bool:
    """Check if Zoom is running using NSWorkspace (faster than AppleScript)."""
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == "us.zoom.xos":
            return True
    return False
```

**Meeting app bundle IDs:**
- Zoom: `us.zoom.xos`
- Zoom Clips: `us.zoom.ZoomClips`
- Microsoft Teams: `com.microsoft.teams2` (new) or `com.microsoft.teams` (classic)
- Slack: `com.tinyspeck.slackmacgap`
- Google Chrome (for Meet): `com.google.Chrome`

---

## Implementation Plan

### Architecture

```
ScreenCapturer (Protocol/ABC)
    |
    +-- CGWindowListCapturer (primary)
    |       Uses CGWindowListCreateImage + CGWindowListCopyWindowInfo
    |       Window-specific capture by window ID
    |       Retina resolution with 50% downscale
    |
    +-- MSSCapturer (fallback)
    |       Uses mss for full-screen capture
    |       Logical resolution (1x)
    |
    +-- ScreenCaptureCLICapturer (emergency fallback)
            Uses screencapture -l for window-specific
            File-based capture with cleanup
```

### Integration with Existing Service

The screen capturer should integrate with the existing `SessionOrchestrator` lifecycle:

1. When `SessionOrchestrator` transitions to `RECORDING`, start periodic screen capture.
2. When `SessionOrchestrator` transitions to `COOLDOWN` or `IDLE`, stop screen capture.
3. Captures run on a separate daemon thread with configurable interval.
4. Captured frames are saved as WebP files in a session-specific directory under `data/screen_captures/`.

### Proposed Configuration (additions to `config.py`)

```python
# Screen capture settings
screen_capture_enabled: bool = False
screen_capture_interval_seconds: float = 15.0
screen_capture_quality: int = 50
screen_capture_scale: float = 0.5
screen_capture_format: str = "webp"
screen_capture_dir: Path = Field(default_factory=lambda: Path("data/screen_captures"))
screen_capture_window_match: str = "zoom"  # Window owner name to match
screen_capture_max_frames_per_session: int = 1000
screen_capture_backend: str = "auto"  # "auto", "cg", "mss", "screencapture"
```

### Performance Budget

At 15-second intervals with the recommended pipeline (CG + 50% resize + WebP q50 m0):

- **CPU**: 156ms every 15s = **1.04% duty cycle** -- negligible impact on audio capture/transcription
- **Memory**: 29 MB peak during capture -- negligible relative to Whisper model
- **Disk**: ~95 KB per frame, ~22 MB per hour of meeting
- **No contention with audio**: Capture runs on a separate thread, uses different system resources (GPU compositor vs audio subsystem)

---

## References

- [macOS Sequoia Screen Recording Prompts (9to5Mac)](https://9to5mac.com/2024/08/06/macos-sequoia-screen-recording-privacy-prompt/)
- [Persistent Content Capture Entitlement (Michael Tsai)](https://mjtsai.com/blog/2024/08/08/sequoia-screen-recording-prompts-and-the-persistent-content-capture-entitlement/)
- [No More Monthly Prompts (Eternal Storms)](https://blog.eternalstorms.at/2024/09/23/psa-no-more-macos-15-sequoia-monthly-screen-recording-permission-reminders/)
- [ScreenCaptureKit on macOS Sonoma (Nonstrict)](https://nonstrict.eu/blog/2023/a-look-at-screencapturekit-on-macos-sonoma/)
- [CGWindowListCreateImage Deprecation (Apple Developer Forums)](https://developer.apple.com/forums/thread/740493)
- [ScreenCaptureKit Documentation (Apple)](https://developer.apple.com/documentation/screencapturekit/)
- [PyObjC ScreenCaptureKit Bindings](https://pyobjc.readthedocs.io/en/latest/apinotes/ScreenCaptureKit.html)
- [PyObjC ScreenCaptureKit Issue #590](https://github.com/ronaldoussoren/pyobjc/issues/590)
- [Fast Screenshot with ScreenCaptureKit (GitHub Gist)](https://gist.github.com/mr-linch/d31024f931441a39c6a830328f8b5030)
- [Python mss Library (GitHub)](https://github.com/BoboTiG/python-mss)
- [Stop macOS 15 Sequoia Monthly Prompts (Lapcatsoftware)](https://lapcatsoftware.com/articles/2024/8/10.html)
- [Disable Sequoia Screen Recording Nag (tinyapps.org)](https://tinyapps.org/blog/202409180700_disable_sequoia_nag.html)
