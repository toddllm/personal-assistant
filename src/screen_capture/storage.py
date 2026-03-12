"""SQLite + filesystem storage for screen captures."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .schemas import CaptureRecord

logger = logging.getLogger(__name__)

_MIGRATION_SQL = """\
CREATE TABLE IF NOT EXISTS screen_captures (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT    NOT NULL,
    captured_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    file_path             TEXT    NOT NULL,
    width                 INTEGER NOT NULL,
    height                INTEGER NOT NULL,
    file_size_bytes       INTEGER NOT NULL,
    ocr_text              TEXT,
    participants_detected TEXT,
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_screen_captures_session
    ON screen_captures (session_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_screen_captures_latest
    ON screen_captures (captured_at DESC);
"""


class CaptureStore:
    """SQLite + filesystem storage for screen captures."""

    def __init__(self, db_path: str | Path = ":memory:", capture_dir: Path | None = None):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._capture_dir = capture_dir or Path("data/screen_captures")
        self._run_migrations()

    def _run_migrations(self):
        migration_file = (
            Path(__file__).resolve().parent.parent.parent
            / "migrations"
            / "002_screen_captures.sql"
        )
        if migration_file.exists():
            sql = migration_file.read_text()
        else:
            sql = _MIGRATION_SQL
        with self._lock:
            self._conn.executescript(sql)

    def save_capture(
        self,
        session_id: str,
        image_data: bytes,
        width: int,
        height: int,
        fmt: str = "webp",
    ) -> CaptureRecord:
        """Save an image to disk and record metadata in SQLite."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        session_dir = self._capture_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        timestamp_slug = now.replace(":", "-").replace(".", "-")
        filename = f"{timestamp_slug}.{fmt}"
        file_path = session_dir / filename
        file_path.write_bytes(image_data)

        rel_path = str(file_path.relative_to(self._capture_dir.parent))
        file_size = len(image_data)

        with self._lock:
            self._conn.execute(
                "INSERT INTO screen_captures "
                "(session_id, captured_at, file_path, width, height, file_size_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, now, rel_path, width, height, file_size),
            )
            self._conn.commit()
            row_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = self._conn.execute(
                "SELECT * FROM screen_captures WHERE id = ?", (row_id,)
            ).fetchone()

        return self._row_to_record(row)

    def list_session(
        self, session_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[CaptureRecord], int]:
        """List captures for a session with pagination."""
        with self._lock:
            total_row = self._conn.execute(
                "SELECT COUNT(*) FROM screen_captures WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            total = total_row[0]
            rows = self._conn.execute(
                "SELECT * FROM screen_captures WHERE session_id = ? "
                "ORDER BY captured_at DESC LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        return [self._row_to_record(r) for r in rows], total

    def get_latest(self) -> CaptureRecord | None:
        """Get the most recent capture across all sessions."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM screen_captures ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def delete_session(self, session_id: str) -> int:
        """Delete all captures for a session, including files."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_path FROM screen_captures WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            cur = self._conn.execute(
                "DELETE FROM screen_captures WHERE session_id = ?",
                (session_id,),
            )
            self._conn.commit()
            deleted = cur.rowcount

        # Clean up files
        session_dir = self._capture_dir / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

        return deleted

    def session_frame_count(self, session_id: str) -> int:
        """Count frames in a session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM screen_captures WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0]

    def close(self):
        self._conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CaptureRecord:
        participants = row["participants_detected"]
        if participants is not None:
            try:
                participants = json.loads(participants)
            except (json.JSONDecodeError, TypeError):
                participants = None

        return CaptureRecord(
            id=row["id"],
            session_id=row["session_id"],
            captured_at=row["captured_at"],
            file_path=row["file_path"],
            width=row["width"],
            height=row["height"],
            file_size_bytes=row["file_size_bytes"],
            ocr_text=row["ocr_text"],
            participants_detected=participants,
        )
