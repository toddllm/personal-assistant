"""Screen capture backends — Protocol + lazy implementations."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class CaptureBackend(Protocol):
    """Mockable interface for taking screen captures."""

    def capture(self) -> tuple[bytes, int, int]:
        """Capture the screen, return (png_bytes, width, height)."""
        ...


class CoreGraphicsBackend:
    """macOS Core Graphics capture via PyObjC (lazy import)."""

    def capture(self) -> tuple[bytes, int, int]:
        try:
            import Quartz
            from AppKit import NSBitmapImageRep, NSPNGFileType
        except ImportError:
            raise ImportError(
                "PyObjC is required for CoreGraphics capture. "
                "Install with: pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa"
            )

        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectInfinite,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if image is None:
            raise RuntimeError("CGWindowListCreateImage returned None")

        width = Quartz.CGImageGetWidth(image)
        height = Quartz.CGImageGetHeight(image)

        rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
        png_data = rep.representationUsingType_properties_(NSPNGFileType, {})
        return bytes(png_data), width, height


class MSSBackend:
    """Cross-platform capture via mss (lazy import)."""

    def capture(self) -> tuple[bytes, int, int]:
        try:
            import mss
            from PIL import Image
        except ImportError:
            raise ImportError(
                "mss and Pillow are required for screen capture. "
                "Install with: pip install 'audio-assist[screen-capture]'"
            )

        import io

        with mss.mss() as sct:
            monitor = sct.monitors[0]  # full virtual screen
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            width, height = img.size
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), width, height


def get_backend() -> CaptureBackend:
    """Return the best available capture backend."""
    # Prefer CoreGraphics on macOS
    try:
        import Quartz  # noqa: F401

        logger.info("Using CoreGraphics capture backend")
        return CoreGraphicsBackend()
    except ImportError:
        pass

    try:
        import mss  # noqa: F401

        logger.info("Using mss capture backend")
        return MSSBackend()
    except ImportError:
        pass

    raise ImportError(
        "No screen capture backend available. "
        "Install PyObjC (macOS) or mss (cross-platform)."
    )
