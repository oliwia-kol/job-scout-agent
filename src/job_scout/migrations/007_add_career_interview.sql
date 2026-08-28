CREATE TABLE IF NOT EXISTS career_interview_sessions (
    session_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    stage TEXT NOT NULL DEFAULT 'cv_audit',
    status TEXT NOT NULL DEFAULT 'active',
    prompt_version TEXT NOT NULL,
    model_config_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS career_interview_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES career_interview_sessions(session_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'pl',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);

CREATE TABLE IF NOT EXISTS career_knowledge_entries (
    entry_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    session_id TEXT REFERENCES career_interview_sessions(session_id),
    category TEXT NOT NULL,
    statement_original TEXT NOT NULL,
    original_language TEXT NOT NULL DEFAULT 'pl',
    canonical_english TEXT,
    provenance TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    superseded_by TEXT REFERENCES career_knowledge_entries(entry_id)
);

CREATE TABLE IF NOT EXISTS career_open_questions (
    question_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    session_id TEXT NOT NULL REFERENCES career_interview_sessions(session_id),
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS career_knowledge_base_versions (
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    version INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    approved_at TEXT,
    PRIMARY KEY (profile_id, version)
);

CREATE INDEX IF NOT EXISTS idx_interview_sessions_profile
ON career_interview_sessions(profile_id, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_interview_messages_session
ON career_interview_messages(session_id, sequence);

CREATE INDEX IF NOT EXISTS idx_knowledge_entries_profile
ON career_knowledge_entries(profile_id, status, category);

CREATE INDEX IF NOT EXISTS idx_open_questions_profile
ON career_open_questions(profile_id, status);
