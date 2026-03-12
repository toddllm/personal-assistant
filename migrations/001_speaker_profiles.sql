-- Speaker profiles for voice enrollment / verification
-- Idempotent: safe to re-run

CREATE TABLE IF NOT EXISTS speaker_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    embedding_blob BLOB   NOT NULL,       -- raw float32 vector (192 × 4 = 768 bytes for ECAPA-TDNN)
    embedding_dim INTEGER NOT NULL DEFAULT 192,
    enrolled_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sample_count  INTEGER NOT NULL DEFAULT 1,
    is_owner      INTEGER NOT NULL DEFAULT 0   -- boolean: 1 = device owner
);

-- Fast lookup for the single owner profile
CREATE INDEX IF NOT EXISTS idx_speaker_profiles_owner
    ON speaker_profiles (is_owner) WHERE is_owner = 1;
