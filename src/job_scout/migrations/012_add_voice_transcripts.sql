CREATE TABLE IF NOT EXISTS voice_transcripts (
    transcript_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    session_id TEXT REFERENCES career_interview_sessions(session_id),
    audio_path TEXT NOT NULL,
    audio_sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    duration_ms INTEGER,
    detected_language TEXT,
    provider TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    corrected_text TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    approved_at TEXT,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_voice_transcripts_profile
ON voice_transcripts(profile_id, created_at);
