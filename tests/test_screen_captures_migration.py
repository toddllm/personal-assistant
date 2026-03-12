"""Tests for migrations/002_screen_captures.sql."""

import json
import sqlite3
from pathlib import Path

import pytest

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "002_screen_captures.sql"


@pytest.fixture()
def db():
    """In-memory SQLite database with the migration applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(MIGRATION.read_text())
    yield conn
    conn.close()


class TestScreenCapturesMigration:
    """Validate schema, CRUD, indexes, and idempotency of screen_captures table."""

    def test_migration_file_exists(self):
        assert MIGRATION.exists(), f"Missing migration: {MIGRATION}"

    def test_table_created(self, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='screen_captures'"
        ).fetchall()
        assert len(rows) == 1

    def test_columns(self, db):
        info = db.execute("PRAGMA table_info(screen_captures)").fetchall()
        col_names = {row[1] for row in info}
        expected = {
            "id", "session_id", "captured_at", "file_path",
            "width", "height", "file_size_bytes",
            "ocr_text", "participants_detected", "created_at",
        }
        assert expected == col_names

    def test_insert_and_select(self, db):
        db.execute(
            """INSERT INTO screen_captures
               (session_id, file_path, width, height, file_size_bytes)
               VALUES (?, ?, ?, ?, ?)""",
            ("sess-001", "/tmp/cap.webp", 1920, 1080, 45000),
        )
        row = db.execute(
            "SELECT id, session_id, width, height, file_size_bytes FROM screen_captures"
        ).fetchone()
        assert row[0] == 1
        assert row[1] == "sess-001"
        assert row[2] == 1920
        assert row[3] == 1080
        assert row[4] == 45000

    def test_captured_at_default(self, db):
        db.execute(
            """INSERT INTO screen_captures
               (session_id, file_path, width, height, file_size_bytes)
               VALUES (?, ?, ?, ?, ?)""",
            ("sess-002", "/tmp/cap2.webp", 1280, 720, 30000),
        )
        captured = db.execute("SELECT captured_at FROM screen_captures").fetchone()[0]
        assert captured is not None
        assert "T" in captured  # ISO-8601

    def test_nullable_ocr_text(self, db):
        db.execute(
            """INSERT INTO screen_captures
               (session_id, file_path, width, height, file_size_bytes, ocr_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("sess-003", "/tmp/cap3.webp", 800, 600, 20000, "Hello world"),
        )
        ocr = db.execute("SELECT ocr_text FROM screen_captures").fetchone()[0]
        assert ocr == "Hello world"

    def test_nullable_participants_detected(self, db):
        participants = json.dumps(["Alice", "Bob"])
        db.execute(
            """INSERT INTO screen_captures
               (session_id, file_path, width, height, file_size_bytes, participants_detected)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("sess-004", "/tmp/cap4.webp", 800, 600, 15000, participants),
        )
        raw = db.execute("SELECT participants_detected FROM screen_captures").fetchone()[0]
        assert json.loads(raw) == ["Alice", "Bob"]

    def test_session_index_exists(self, db):
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='screen_captures'"
        ).fetchall()
        idx_names = {r[0] for r in indexes}
        assert "idx_screen_captures_session" in idx_names

    def test_latest_index_exists(self, db):
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='screen_captures'"
        ).fetchall()
        idx_names = {r[0] for r in indexes}
        assert "idx_screen_captures_latest" in idx_names

    def test_idempotent_rerun(self, db):
        """Running the migration a second time should not raise."""
        db.executescript(MIGRATION.read_text())
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='screen_captures'"
        ).fetchall()
        assert len(rows) == 1
