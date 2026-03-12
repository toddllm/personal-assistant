"""Tests for migrations/001_speaker_profiles.sql."""

import sqlite3
import struct
from pathlib import Path

import pytest

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "001_speaker_profiles.sql"


@pytest.fixture()
def db():
    """In-memory SQLite database with the migration applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(MIGRATION.read_text())
    yield conn
    conn.close()


class TestSpeakerProfilesMigration:
    """Validate schema, CRUD, and idempotency of speaker_profiles table."""

    def test_migration_file_exists(self):
        assert MIGRATION.exists(), f"Missing migration: {MIGRATION}"

    def test_table_created(self, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='speaker_profiles'"
        ).fetchall()
        assert len(rows) == 1

    def test_columns(self, db):
        info = db.execute("PRAGMA table_info(speaker_profiles)").fetchall()
        col_names = {row[1] for row in info}
        expected = {
            "id", "name", "embedding_blob", "embedding_dim",
            "enrolled_at", "sample_count", "is_owner",
        }
        assert expected == col_names

    def test_insert_and_select(self, db):
        embedding = struct.pack("192f", *([0.1] * 192))
        db.execute(
            "INSERT INTO speaker_profiles (name, embedding_blob) VALUES (?, ?)",
            ("Alice", embedding),
        )
        row = db.execute("SELECT id, name, embedding_dim, sample_count, is_owner FROM speaker_profiles").fetchone()
        assert row[0] == 1         # auto-increment id
        assert row[1] == "Alice"
        assert row[2] == 192       # default embedding_dim
        assert row[3] == 1         # default sample_count
        assert row[4] == 0         # default is_owner

    def test_enrolled_at_default(self, db):
        embedding = struct.pack("192f", *([0.0] * 192))
        db.execute(
            "INSERT INTO speaker_profiles (name, embedding_blob) VALUES (?, ?)",
            ("Bob", embedding),
        )
        enrolled = db.execute("SELECT enrolled_at FROM speaker_profiles WHERE name='Bob'").fetchone()[0]
        assert enrolled is not None
        assert "T" in enrolled  # ISO-8601 format

    def test_is_owner_index_exists(self, db):
        indexes = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='speaker_profiles'"
        ).fetchall()
        idx_names = {r[0] for r in indexes}
        assert "idx_speaker_profiles_owner" in idx_names

    def test_idempotent_rerun(self, db):
        """Running the migration a second time should not raise."""
        db.executescript(MIGRATION.read_text())
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='speaker_profiles'"
        ).fetchall()
        assert len(rows) == 1

    def test_delete_profile(self, db):
        embedding = struct.pack("192f", *([0.5] * 192))
        db.execute(
            "INSERT INTO speaker_profiles (name, embedding_blob, is_owner) VALUES (?, ?, 1)",
            ("Owner", embedding),
        )
        assert db.execute("SELECT COUNT(*) FROM speaker_profiles").fetchone()[0] == 1
        db.execute("DELETE FROM speaker_profiles WHERE name='Owner'")
        assert db.execute("SELECT COUNT(*) FROM speaker_profiles").fetchone()[0] == 0
