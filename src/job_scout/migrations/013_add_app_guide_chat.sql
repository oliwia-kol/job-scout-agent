CREATE TABLE IF NOT EXISTS app_guide_sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_guide_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES app_guide_sessions(session_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_app_guide_messages_session
ON app_guide_messages(session_id, sequence);
