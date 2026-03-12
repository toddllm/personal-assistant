"""Speaker enrollment CLI — enroll voices into SQLite profiles.

Provides WAV validation, embedding extraction (via SpeechBrain), and a
ProfileStore backed by SQLite.  All heavy imports (speechbrain, torchaudio)
are lazy so tests can run with a FakeExtractor.
"""

from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_DIM = 192
EMBEDDING_BYTES = EMBEDDING_DIM * 4  # float32

_MIGRATION_SQL = """\
CREATE TABLE IF NOT EXISTS speaker_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    embedding_blob BLOB   NOT NULL,
    embedding_dim INTEGER NOT NULL DEFAULT 192,
    enrolled_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sample_count  INTEGER NOT NULL DEFAULT 1,
    is_owner      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_speaker_profiles_owner
    ON speaker_profiles (is_owner) WHERE is_owner = 1;
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AudioValidation:
    """Result of WAV file validation."""

    valid: bool
    reason: str | None = None
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0


@dataclass
class SpeakerProfile:
    """Row from the speaker_profiles table."""

    id: int
    name: str
    embedding_dim: int
    enrolled_at: str
    sample_count: int
    is_owner: bool


@dataclass
class StoredSpeakerProfile:
    """Speaker profile metadata plus the stored embedding vector."""

    profile: SpeakerProfile
    embedding: np.ndarray


# ---------------------------------------------------------------------------
# Embedding serialisation
# ---------------------------------------------------------------------------


def embedding_to_blob(embedding: np.ndarray) -> bytes:
    """Pack a 192-dim float32 vector into a 768-byte BLOB."""
    if embedding.shape != (EMBEDDING_DIM,):
        raise ValueError(f"Expected shape ({EMBEDDING_DIM},), got {embedding.shape}")
    return struct.pack(f"<{EMBEDDING_DIM}f", *embedding.astype(np.float32))


def blob_to_embedding(blob: bytes) -> np.ndarray:
    """Unpack a 768-byte BLOB into a 192-dim float32 vector."""
    if len(blob) != EMBEDDING_BYTES:
        raise ValueError(f"Expected {EMBEDDING_BYTES} bytes, got {len(blob)}")
    return np.array(struct.unpack(f"<{EMBEDDING_DIM}f", blob), dtype=np.float32)


# ---------------------------------------------------------------------------
# WAV validation
# ---------------------------------------------------------------------------


def validate_wav(path: Path, min_seconds: float = 10.0, max_seconds: float = 30.0) -> AudioValidation:
    """Validate a WAV file for enrollment suitability."""
    if not path.exists():
        return AudioValidation(valid=False, reason="File does not exist")
    if path.suffix.lower() != ".wav":
        return AudioValidation(valid=False, reason="File is not a .wav file")

    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            n_frames = wf.getnframes()
            duration = n_frames / sr

            if sw != 2:
                return AudioValidation(
                    valid=False,
                    reason=f"Expected 16-bit samples, got {sw * 8}-bit",
                    duration=duration,
                    sample_rate=sr,
                    channels=ch,
                )

            if duration < min_seconds:
                return AudioValidation(
                    valid=False,
                    reason=f"Too short: {duration:.1f}s (minimum {min_seconds}s)",
                    duration=duration,
                    sample_rate=sr,
                    channels=ch,
                )
            if duration > max_seconds:
                return AudioValidation(
                    valid=False,
                    reason=f"Too long: {duration:.1f}s (maximum {max_seconds}s)",
                    duration=duration,
                    sample_rate=sr,
                    channels=ch,
                )

            # RMS silence check
            raw = wf.readframes(n_frames)
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
            rms = np.sqrt(np.mean(samples**2))
            if rms < 50.0:
                return AudioValidation(
                    valid=False,
                    reason="Audio is silent or near-silent",
                    duration=duration,
                    sample_rate=sr,
                    channels=ch,
                )

            return AudioValidation(
                valid=True, duration=duration, sample_rate=sr, channels=ch
            )
    except wave.Error as exc:
        return AudioValidation(valid=False, reason=f"Invalid WAV: {exc}")


# ---------------------------------------------------------------------------
# Embedding extractor protocol + SpeechBrain implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingExtractor(Protocol):
    """Mockable interface for extracting speaker embeddings."""

    def extract(self, wav_path: Path) -> np.ndarray: ...


class SpeechBrainExtractor:
    """Extracts ECAPA-TDNN embeddings via speechbrain (lazy imports)."""

    def __init__(self, model_source: str = "speechbrain/spkrec-ecapa-voxceleb"):
        self._model_source = model_source
        self._classifier = None

    def _load(self):
        if self._classifier is not None:
            return
        try:
            from speechbrain.inference.speaker import SpeakerRecognition
        except ImportError:
            raise ImportError(
                "speechbrain is required for embedding extraction. "
                "Install with: pip install 'audio-assist[speaker]'"
            )
        self._classifier = SpeakerRecognition.from_hparams(
            source=self._model_source,
            savedir=f"/tmp/speechbrain_models/{self._model_source.replace('/', '_')}",
        )

    def extract(self, wav_path: Path) -> np.ndarray:
        self._load()
        assert self._classifier is not None
        embedding = self._classifier.encode_batch(
            self._classifier.load_audio(str(wav_path)).unsqueeze(0)
        )
        vec = embedding.squeeze().cpu().numpy().astype(np.float32)
        if vec.shape != (EMBEDDING_DIM,):
            raise ValueError(
                f"Model returned {vec.shape} embedding, expected ({EMBEDDING_DIM},)"
            )
        return vec


# ---------------------------------------------------------------------------
# Profile store (SQLite)
# ---------------------------------------------------------------------------


class ProfileStore:
    """SQLite wrapper for speaker profiles."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._run_migrations()

    def _run_migrations(self):
        migration_file = Path(__file__).resolve().parent.parent.parent / "migrations" / "001_speaker_profiles.sql"
        if migration_file.exists():
            sql = migration_file.read_text()
        else:
            sql = _MIGRATION_SQL
        with self._lock:
            self._conn.executescript(sql)

    def upsert(self, name: str, embedding: np.ndarray, is_owner: bool = False) -> SpeakerProfile:
        """Insert or weighted-average update a speaker profile."""
        blob = embedding_to_blob(embedding)
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, embedding_blob, sample_count, is_owner FROM speaker_profiles WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
            if row is not None:
                # Weighted average
                old_emb = blob_to_embedding(row["embedding_blob"])
                old_count = row["sample_count"]
                new_count = old_count + 1
                averaged = (old_emb * old_count + embedding) / new_count
                # Re-normalise
                norm = np.linalg.norm(averaged)
                if norm > 0:
                    averaged = averaged / norm
                new_blob = embedding_to_blob(averaged)
                self._conn.execute(
                    "UPDATE speaker_profiles SET embedding_blob = ?, sample_count = ?, "
                    "is_owner = ? WHERE id = ?",
                    (new_blob, new_count, int(is_owner or bool(row["is_owner"] if "is_owner" in row.keys() else False)), row["id"]),
                )
                self._conn.commit()
                return self.get_by_id(row["id"])  # type: ignore[return-value]
            else:
                # Auto-owner: if no owner exists, first profile is owner
                if not is_owner and not self.has_owner():
                    is_owner = True
                self._conn.execute(
                    "INSERT INTO speaker_profiles (name, embedding_blob, embedding_dim, sample_count, is_owner) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (name, blob, EMBEDDING_DIM, int(is_owner)),
                )
                self._conn.commit()
                profile_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                return self.get_by_id(profile_id)  # type: ignore[return-value]

    def get_by_id(self, profile_id: int) -> SpeakerProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM speaker_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    def get_by_name(self, name: str) -> SpeakerProfile | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM speaker_profiles WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    def list_all(self) -> list[SpeakerProfile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM speaker_profiles ORDER BY id"
            ).fetchall()
        return [self._row_to_profile(r) for r in rows]

    def list_all_with_embeddings(self) -> list[StoredSpeakerProfile]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM speaker_profiles ORDER BY id"
            ).fetchall()
        profiles: list[StoredSpeakerProfile] = []
        for row in rows:
            profiles.append(
                StoredSpeakerProfile(
                    profile=self._row_to_profile(row),
                    embedding=blob_to_embedding(row["embedding_blob"]),
                )
            )
        return profiles

    def delete(self, profile_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM speaker_profiles WHERE id = ?", (profile_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def has_owner(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM speaker_profiles WHERE is_owner = 1 LIMIT 1"
            ).fetchone()
        return row is not None

    def close(self):
        self._conn.close()

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> SpeakerProfile:
        return SpeakerProfile(
            id=row["id"],
            name=row["name"],
            embedding_dim=row["embedding_dim"],
            enrolled_at=row["enrolled_at"],
            sample_count=row["sample_count"],
            is_owner=bool(row["is_owner"]),
        )


# ---------------------------------------------------------------------------
# Enrollment logic
# ---------------------------------------------------------------------------


def enroll_speaker(
    name: str,
    sample_paths: list[Path],
    *,
    is_owner: bool = False,
    db_path: str | Path = "data/speaker_profiles.db",
    model_source: str = "speechbrain/spkrec-ecapa-voxceleb",
    extractor: EmbeddingExtractor | None = None,
    store: ProfileStore | None = None,
) -> SpeakerProfile:
    """Validate audio, extract embeddings, average, normalise, upsert."""
    if len(sample_paths) < 1:
        raise ValueError("At least one audio sample is required")
    if len(sample_paths) > 20:
        raise ValueError("Too many samples (maximum 20)")

    # Validate all samples
    for p in sample_paths:
        result = validate_wav(p)
        if not result.valid:
            raise ValueError(f"Invalid audio {p.name}: {result.reason}")

    # Extract embeddings
    if extractor is None:
        extractor = SpeechBrainExtractor(model_source)
    embeddings = [extractor.extract(p) for p in sample_paths]

    # Average and normalise
    avg = np.mean(embeddings, axis=0).astype(np.float32)
    norm = np.linalg.norm(avg)
    if norm > 0:
        avg = avg / norm

    # Store
    if store is None:
        store = ProfileStore(db_path)
    return store.upsert(name, avg, is_owner=is_owner)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="speaker-enroll",
        description="Enroll a speaker from WAV samples",
    )
    parser.add_argument("--name", required=True, help="Speaker name")
    parser.add_argument(
        "--samples",
        required=True,
        help="Directory of WAV files, or comma-separated file paths",
    )
    parser.add_argument("--owner", action="store_true", help="Mark as device owner")
    parser.add_argument(
        "--db", default="data/speaker_profiles.db", help="SQLite database path"
    )
    parser.add_argument(
        "--model",
        default="speechbrain/spkrec-ecapa-voxceleb",
        help="SpeechBrain model source",
    )

    args = parser.parse_args(argv)

    # Resolve sample paths
    samples_arg = args.samples
    samples_path = Path(samples_arg)
    if samples_path.is_dir():
        sample_files = sorted(samples_path.glob("*.wav"))
        if not sample_files:
            print(f"Error: no .wav files found in {samples_path}", file=sys.stderr)
            sys.exit(1)
    else:
        sample_files = [Path(s.strip()) for s in samples_arg.split(",")]

    # Ensure db directory exists
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    profile = enroll_speaker(
        name=args.name,
        sample_paths=sample_files,
        is_owner=args.owner,
        db_path=db_path,
        model_source=args.model,
    )

    print(f"Enrolled '{profile.name}' (id={profile.id}, samples={profile.sample_count}, owner={profile.is_owner})")


if __name__ == "__main__":
    main()
