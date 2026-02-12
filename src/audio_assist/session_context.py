"""Thread-safe session context for correlation IDs in logging."""

from __future__ import annotations

import logging
import threading

_context = threading.local()


def set_session_id(session_id: str | None) -> None:
    """Set the active meeting session ID for the current thread."""
    _context.session_id = session_id


def get_session_id() -> str | None:
    """Get the active meeting session ID, or None."""
    return getattr(_context, "session_id", None)


class SessionContextFilter(logging.Filter):
    """Inject session_id into every log record for correlation."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = get_session_id() or "-"  # type: ignore[attr-defined]
        return True
