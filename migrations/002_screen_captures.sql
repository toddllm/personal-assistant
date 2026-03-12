-- Screen captures storage for the screen-capture microservice
-- Idempotent: safe to re-run

CREATE TABLE IF NOT EXISTS screen_captures (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            TEXT    NOT NULL,
    captured_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    file_path             TEXT    NOT NULL,
    width                 INTEGER NOT NULL,
    height                INTEGER NOT NULL,
    file_size_bytes       INTEGER NOT NULL,
    ocr_text              TEXT,           -- extracted text, populated asynchronously
    participants_detected TEXT,           -- JSON array of participant names, nullable
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Session-scoped queries ordered by capture time (most recent first)
CREATE INDEX IF NOT EXISTS idx_screen_captures_session
    ON screen_captures (session_id, captured_at DESC);

-- Global latest-capture queries
CREATE INDEX IF NOT EXISTS idx_screen_captures_latest
    ON screen_captures (captured_at DESC);
