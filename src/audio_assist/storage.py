from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Iterable


@dataclass(slots=True)
class TranscriptRecord:
    id: int
    source_id: str
    session_id: str | None
    started_at: datetime
    ended_at: datetime
    text: str
    speaker: str | None = None


@dataclass(slots=True)
class SessionRecord:
    id: int
    session_id: str
    source_id: str
    source_type: str | None
    started_at: datetime
    ended_at: datetime
    text: str
    speaker: str | None = None


class TranscriptStore:
    def __init__(self, database_path: Path):
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    session_id TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    speaker TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transcript_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL,
                    source_type TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    speaker TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
                    text,
                    content='transcripts',
                    content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS transcripts_ai AFTER INSERT ON transcripts BEGIN
                    INSERT INTO transcripts_fts(rowid, text) VALUES (new.id, new.text);
                END;

                CREATE TRIGGER IF NOT EXISTS transcripts_ad AFTER DELETE ON transcripts BEGIN
                    INSERT INTO transcripts_fts(transcripts_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
                END;

                CREATE TRIGGER IF NOT EXISTS transcripts_au AFTER UPDATE ON transcripts BEGIN
                    INSERT INTO transcripts_fts(transcripts_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
                    INSERT INTO transcripts_fts(rowid, text) VALUES (new.id, new.text);
                END;
                """
            )
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(transcripts)").fetchall()
            }
            if "speaker" not in columns:
                self._conn.execute("ALTER TABLE transcripts ADD COLUMN speaker TEXT")
            if "session_id" not in columns:
                self._conn.execute("ALTER TABLE transcripts ADD COLUMN session_id TEXT")

    def add(
        self,
        source_id: str,
        started_at: datetime,
        ended_at: datetime,
        text: str,
        speaker: str | None = None,
        session_id: str | None = None,
        source_type: str | None = None,
    ) -> TranscriptRecord:
        if not text.strip():
            raise ValueError("Transcript text must be non-empty.")
        speaker_norm = str(speaker).strip()[:64] if speaker else None
        session_id_norm = str(session_id).strip()[:96] if session_id else None
        started_at_utc = started_at.astimezone(UTC)
        ended_at_utc = ended_at.astimezone(UTC)
        text_norm = text.strip()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO transcripts (source_id, session_id, started_at, ended_at, text, speaker)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    session_id_norm,
                    started_at_utc.isoformat(),
                    ended_at_utc.isoformat(),
                    text_norm,
                    speaker_norm,
                ),
            )
            record_id = int(cursor.lastrowid)
            if session_id_norm:
                self._append_session_text_locked(
                    session_id=session_id_norm,
                    source_id=source_id,
                    source_type=source_type,
                    started_at=started_at_utc,
                    ended_at=ended_at_utc,
                    text=text_norm,
                    speaker=speaker_norm,
                )
        return TranscriptRecord(
            id=record_id,
            source_id=source_id,
            session_id=session_id_norm,
            started_at=started_at_utc,
            ended_at=ended_at_utc,
            text=text_norm,
            speaker=speaker_norm,
        )

    def open_session(
        self,
        session_id: str,
        source_id: str,
        source_type: str,
        started_at: datetime,
        speaker: str | None = None,
    ) -> SessionRecord:
        session_id_norm = str(session_id).strip()[:96]
        speaker_norm = str(speaker).strip()[:64] if speaker else None
        started_at_utc = started_at.astimezone(UTC)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO transcript_sessions (
                    session_id, source_id, source_type, started_at, ended_at, text, speaker, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (
                    session_id_norm,
                    source_id,
                    source_type,
                    started_at_utc.isoformat(),
                    started_at_utc.isoformat(),
                    speaker_norm,
                ),
            )
            row = self._conn.execute(
                """
                SELECT id, session_id, source_id, source_type, started_at, ended_at, text, speaker
                FROM transcript_sessions
                WHERE session_id = ?
                """,
                (session_id_norm,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Could not open transcript session '{session_id_norm}'.")
        return self._to_session_record(row)

    def close_session(self, session_id: str, ended_at: datetime) -> bool:
        session_id_norm = str(session_id).strip()[:96]
        ended_at_utc = ended_at.astimezone(UTC)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE transcript_sessions
                SET ended_at = CASE WHEN ended_at < ? THEN ? ELSE ended_at END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (ended_at_utc.isoformat(), ended_at_utc.isoformat(), session_id_norm),
            )
            return cursor.rowcount > 0

    def recent_sessions(
        self,
        limit: int = 50,
        source_id: str | None = None,
        since_seconds: int | None = None,
    ) -> list[SessionRecord]:
        filters: list[str] = ["text <> ''"]
        params: list[object] = []
        if source_id:
            filters.append("source_id = ?")
            params.append(source_id)
        if since_seconds:
            cutoff = datetime.now(tz=UTC) - timedelta(seconds=since_seconds)
            filters.append("ended_at >= ?")
            params.append(cutoff.isoformat())
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT id, session_id, source_id, source_type, started_at, ended_at, text, speaker
            FROM transcript_sessions
            {where_clause}
            ORDER BY ended_at DESC
            LIMIT ?
        """
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._to_session_record(row) for row in rows]

    def page_sessions(
        self,
        limit: int = 200,
        before_id: int | None = None,
        source_id: str | None = None,
        since_seconds: int | None = None,
    ) -> tuple[list[SessionRecord], int | None, bool]:
        safe_limit = max(1, min(limit, 1000))
        filters: list[str] = ["text <> ''"]
        params: list[object] = []
        if before_id is not None:
            filters.append("id < ?")
            params.append(before_id)
        if source_id:
            filters.append("source_id = ?")
            params.append(source_id)
        if since_seconds:
            cutoff = datetime.now(tz=UTC) - timedelta(seconds=since_seconds)
            filters.append("ended_at >= ?")
            params.append(cutoff.isoformat())

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT id, session_id, source_id, source_type, started_at, ended_at, text, speaker
            FROM transcript_sessions
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(safe_limit + 1)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        has_more = len(rows) > safe_limit
        if has_more:
            rows = rows[:safe_limit]
        next_before_id = int(rows[-1]["id"]) if (has_more and rows) else None
        return [self._to_session_record(row) for row in rows], next_before_id, has_more

    def recent(
        self,
        limit: int = 50,
        source_id: str | None = None,
        since_seconds: int | None = None,
    ) -> list[TranscriptRecord]:
        filters: list[str] = []
        params: list[object] = []
        if source_id:
            filters.append("source_id = ?")
            params.append(source_id)
        if since_seconds:
            cutoff = datetime.now(tz=UTC) - timedelta(seconds=since_seconds)
            filters.append("ended_at >= ?")
            params.append(cutoff.isoformat())

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT id, source_id, session_id, started_at, ended_at, text, speaker
            FROM transcripts
            {where_clause}
            ORDER BY ended_at DESC
            LIMIT ?
        """
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._to_record(row) for row in rows]

    def recent_compact(
        self,
        limit: int = 50,
        source_id: str | None = None,
        since_seconds: int | None = None,
    ) -> list[TranscriptRecord]:
        raw_limit = max(limit * 6, limit)
        records = self.recent(limit=raw_limit, source_id=source_id, since_seconds=since_seconds)
        return self._compact_records(records, target_limit=limit)

    def page(
        self,
        limit: int = 200,
        before_id: int | None = None,
        source_id: str | None = None,
        since_seconds: int | None = None,
        compact: bool = False,
    ) -> tuple[list[TranscriptRecord], int | None, bool]:
        safe_limit = max(1, min(limit, 1000))
        filters: list[str] = []
        params: list[object] = []
        if before_id is not None:
            filters.append("id < ?")
            params.append(before_id)
        if source_id:
            filters.append("source_id = ?")
            params.append(source_id)
        if since_seconds:
            cutoff = datetime.now(tz=UTC) - timedelta(seconds=since_seconds)
            filters.append("ended_at >= ?")
            params.append(cutoff.isoformat())

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT id, source_id, session_id, started_at, ended_at, text, speaker
            FROM transcripts
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(safe_limit + 1)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        has_more = len(rows) > safe_limit
        if has_more:
            rows = rows[:safe_limit]
        oldest_raw_id = int(rows[-1]["id"]) if rows else None
        records = [self._to_record(row) for row in rows]
        if compact:
            records = self._compact_records(records, target_limit=safe_limit)
        next_before_id = oldest_raw_id if has_more else None
        return records, next_before_id, has_more

    def list_sources(self, since_seconds: int | None = None) -> list[str]:
        params: list[object] = []
        where = ""
        if since_seconds:
            cutoff = datetime.now(tz=UTC) - timedelta(seconds=since_seconds)
            where = "WHERE ended_at >= ?"
            params.append(cutoff.isoformat())
        session_query = f"""
            SELECT source_id, MAX(ended_at) AS latest
            FROM transcript_sessions
            {"WHERE text <> ''" + (" AND ended_at >= ?" if since_seconds else "")}
            GROUP BY source_id
        """
        chunk_query = f"""
            SELECT source_id, MAX(ended_at) AS latest
            FROM transcripts
            {where}
            GROUP BY source_id
        """
        with self._lock:
            session_rows = self._conn.execute(session_query, params).fetchall()
            chunk_rows = self._conn.execute(chunk_query, params).fetchall()
        latest_by_source: dict[str, str] = {}
        for row in [*session_rows, *chunk_rows]:
            source = str(row["source_id"])
            latest = str(row["latest"])
            if source not in latest_by_source or latest_by_source[source] < latest:
                latest_by_source[source] = latest
        return sorted(latest_by_source.keys(), key=lambda item: latest_by_source[item], reverse=True)

    def search(
        self,
        query_text: str,
        limit: int = 8,
        source_id: str | None = None,
        since_seconds: int | None = None,
    ) -> list[TranscriptRecord]:
        escaped = self._escape_fts(query_text)
        cutoff_iso = None
        if since_seconds:
            cutoff_iso = (datetime.now(tz=UTC) - timedelta(seconds=since_seconds)).isoformat()

        # Pull hits via FTS, then expand into local neighborhood chunks for continuity.
        hit_limit = max(limit * 4, 24)
        neighbor_radius = 2
        context_limit = max(limit * 8, 40)

        sql = """
            WITH hits AS (
                SELECT
                    t.id AS hit_id,
                    t.source_id AS source_id,
                    t.ended_at AS hit_ended_at
                FROM transcripts_fts
                JOIN transcripts t ON t.id = transcripts_fts.rowid
                WHERE transcripts_fts MATCH ?
                  AND (? IS NULL OR t.source_id = ?)
                  AND (? IS NULL OR t.ended_at >= ?)
                ORDER BY bm25(transcripts_fts)
                LIMIT ?
            ),
            ctx AS (
                SELECT
                    t2.id AS id,
                    t2.source_id AS source_id,
                    t2.started_at AS started_at,
                    t2.ended_at AS ended_at,
                    t2.text AS text,
                    t2.session_id AS session_id,
                    t2.speaker AS speaker,
                    MIN(ABS(t2.id - hits.hit_id)) AS distance
                FROM hits
                JOIN transcripts t2
                  ON t2.source_id = hits.source_id
                 AND t2.id BETWEEN hits.hit_id - ? AND hits.hit_id + ?
                WHERE (? IS NULL OR t2.source_id = ?)
                  AND (? IS NULL OR t2.ended_at >= ?)
                GROUP BY t2.id, t2.source_id, t2.session_id, t2.started_at, t2.ended_at, t2.text, t2.speaker
            )
            SELECT id, source_id, session_id, started_at, ended_at, text, speaker
            FROM ctx
            ORDER BY distance ASC, ended_at DESC
            LIMIT ?
        """
        params: list[object] = [
            escaped,
            source_id,
            source_id,
            cutoff_iso,
            cutoff_iso,
            hit_limit,
            neighbor_radius,
            neighbor_radius,
            source_id,
            source_id,
            cutoff_iso,
            cutoff_iso,
            context_limit,
        ]

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        if not rows:
            return self.recent_compact(limit=limit, source_id=source_id, since_seconds=since_seconds)
        records = [self._to_record(row) for row in rows]
        return self._compact_records(records, target_limit=limit)

    def update_speaker(
        self,
        transcript_id: int,
        speaker: str,
        *,
        overwrite: bool = False,
    ) -> bool:
        speaker_norm = str(speaker or "").strip()[:64]
        if not speaker_norm:
            return False
        with self._lock, self._conn:
            if overwrite:
                cursor = self._conn.execute(
                    """
                    UPDATE transcripts
                    SET speaker = ?
                    WHERE id = ?
                    """,
                    (speaker_norm, transcript_id),
                )
            else:
                cursor = self._conn.execute(
                    """
                    UPDATE transcripts
                    SET speaker = ?
                    WHERE id = ?
                      AND (speaker IS NULL OR TRIM(speaker) = '')
                    """,
                    (speaker_norm, transcript_id),
                )
            return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _compact_records(
        self,
        records: Iterable[TranscriptRecord],
        target_limit: int,
        merge_gap_seconds: float = 1.4,
        max_segments_per_group: int = 6,
        max_chars_per_group: int = 520,
    ) -> list[TranscriptRecord]:
        by_id: dict[int, TranscriptRecord] = {}
        for record in records:
            by_id[record.id] = record
        if not by_id:
            return []

        ordered = sorted(by_id.values(), key=lambda item: (item.source_id, item.started_at))
        merged: list[TranscriptRecord] = []

        current_source: str | None = None
        current_session_id: str | None = None
        current_speaker: str | None = None
        current_start: datetime | None = None
        current_end: datetime | None = None
        current_parts: list[str] = []
        current_last_id: int | None = None
        current_segments = 0
        current_chars = 0

        def flush() -> None:
            nonlocal current_source, current_session_id, current_start, current_end, current_parts
            nonlocal current_speaker
            nonlocal current_last_id, current_segments, current_chars
            if not current_source or current_start is None or current_end is None or current_last_id is None:
                return
            text = " ".join(part for part in current_parts if part).strip()
            if text:
                merged.append(
                    TranscriptRecord(
                        id=current_last_id,
                        source_id=current_source,
                        session_id=current_session_id,
                        started_at=current_start,
                        ended_at=current_end,
                        text=text,
                        speaker=current_speaker,
                    )
                )
            current_source = None
            current_session_id = None
            current_speaker = None
            current_start = None
            current_end = None
            current_parts = []
            current_last_id = None
            current_segments = 0
            current_chars = 0

        for record in ordered:
            gap_seconds = None
            if current_end is not None:
                gap_seconds = (record.started_at - current_end).total_seconds()

            can_merge = (
                current_source == record.source_id
                and current_session_id == record.session_id
                and current_speaker == record.speaker
                and gap_seconds is not None
                and gap_seconds <= merge_gap_seconds
                and current_segments < max_segments_per_group
                and (current_chars + len(record.text) + 1) <= max_chars_per_group
            )

            if not can_merge:
                flush()
                current_source = record.source_id
                current_session_id = record.session_id
                current_speaker = record.speaker
                current_start = record.started_at
                current_end = record.ended_at
                current_last_id = record.id
                current_segments = 0
                current_chars = 0
                current_parts = []

            self._append_text_part(current_parts, record.text)
            current_segments += 1
            current_chars += len(record.text)
            current_end = record.ended_at
            current_last_id = record.id
            if current_start is None:
                current_start = record.started_at

        flush()
        merged.sort(key=lambda item: item.ended_at, reverse=True)
        return merged[:target_limit]

    @classmethod
    def _append_text_part(cls, parts: list[str], new_text: str) -> None:
        text = new_text.strip()
        if not text:
            return
        if not parts:
            parts.append(text)
            return

        prev = parts[-1]
        prev_l = prev.lower()
        text_l = text.lower()
        if text_l == prev_l:
            return
        if text_l in prev_l and len(text_l) <= len(prev_l):
            return
        if prev_l in text_l and len(text_l) > len(prev_l):
            parts[-1] = text
            return

        overlap = cls._overlap_chars(prev_l, text_l, max_chars=80)
        if overlap >= 8:
            trimmed = text[overlap:].lstrip(" ,.;:-")
            if not trimmed:
                return
            parts.append(trimmed)
            return
        parts.append(text)

    def _append_session_text_locked(
        self,
        session_id: str,
        source_id: str,
        source_type: str | None,
        started_at: datetime,
        ended_at: datetime,
        text: str,
        speaker: str | None,
    ) -> None:
        row = self._conn.execute(
            """
            SELECT id, session_id, source_id, source_type, started_at, ended_at, text, speaker
            FROM transcript_sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO transcript_sessions (
                    session_id, source_id, source_type, started_at, ended_at, text, speaker, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    session_id,
                    source_id,
                    source_type,
                    started_at.isoformat(),
                    ended_at.isoformat(),
                    text,
                    speaker,
                ),
            )
            return

        existing_text = str(row["text"] or "")
        merged_text = self._merge_session_text(existing_text, text)
        existing_started = datetime.fromisoformat(str(row["started_at"])).astimezone(UTC)
        existing_ended = datetime.fromisoformat(str(row["ended_at"])).astimezone(UTC)
        updated_started = min(existing_started, started_at)
        updated_ended = max(existing_ended, ended_at)
        updated_speaker = str(row["speaker"]).strip() if row["speaker"] else speaker
        updated_source_type = str(row["source_type"]).strip() if row["source_type"] else source_type

        self._conn.execute(
            """
            UPDATE transcript_sessions
            SET source_id = ?,
                source_type = ?,
                started_at = ?,
                ended_at = ?,
                text = ?,
                speaker = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
            """,
            (
                source_id,
                updated_source_type,
                updated_started.isoformat(),
                updated_ended.isoformat(),
                merged_text,
                updated_speaker,
                session_id,
            ),
        )

    @classmethod
    def _merge_session_text(cls, existing_text: str, new_text: str) -> str:
        incoming = new_text.strip()
        if not incoming:
            return existing_text
        existing = existing_text.strip()
        if not existing:
            return incoming

        existing_tail = existing[-640:]
        existing_tail_l = existing_tail.lower()
        incoming_l = incoming.lower()

        if incoming_l == existing_tail_l:
            return existing
        if incoming_l in existing_tail_l and len(incoming_l) <= len(existing_tail_l):
            return existing

        overlap = cls._overlap_chars(existing_tail_l, incoming_l, max_chars=120)
        if overlap >= 8:
            trimmed = incoming[overlap:].lstrip(" ,.;:-")
            if not trimmed:
                return existing
            return f"{existing} {trimmed}"
        return f"{existing} {incoming}"

    @staticmethod
    def _overlap_chars(left: str, right: str, max_chars: int = 80) -> int:
        bound = min(len(left), len(right), max_chars)
        for size in range(bound, 7, -1):
            if left[-size:] == right[:size]:
                return size
        return 0

    @staticmethod
    def _escape_fts(text: str) -> str:
        tokens = [tok for tok in text.replace('"', " ").split() if tok]
        if not tokens:
            return '""'
        return " OR ".join(f'"{tok}"' for tok in tokens)

    @staticmethod
    def _to_record(row: sqlite3.Row) -> TranscriptRecord:
        return TranscriptRecord(
            id=int(row["id"]),
            source_id=str(row["source_id"]),
            session_id=str(row["session_id"]).strip() if row["session_id"] else None,
            started_at=datetime.fromisoformat(str(row["started_at"])).astimezone(UTC),
            ended_at=datetime.fromisoformat(str(row["ended_at"])).astimezone(UTC),
            text=str(row["text"]),
            speaker=str(row["speaker"]).strip() if row["speaker"] else None,
        )

    @staticmethod
    def _to_session_record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            source_id=str(row["source_id"]),
            source_type=str(row["source_type"]).strip() if row["source_type"] else None,
            started_at=datetime.fromisoformat(str(row["started_at"])).astimezone(UTC),
            ended_at=datetime.fromisoformat(str(row["ended_at"])).astimezone(UTC),
            text=str(row["text"]),
            speaker=str(row["speaker"]).strip() if row["speaker"] else None,
        )
