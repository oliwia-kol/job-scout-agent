CREATE TABLE IF NOT EXISTS offer_chat_sessions (
    session_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    comparison_offer_id INTEGER REFERENCES offers(id),
    status TEXT NOT NULL DEFAULT 'active',
    prompt_version TEXT NOT NULL,
    model_config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_chat_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES offer_chat_sessions(session_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);

CREATE TABLE IF NOT EXISTS offer_chat_profile_proposals (
    proposal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES offer_chat_sessions(session_id),
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    statement TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_offer_chat_profile_offer
ON offer_chat_sessions(profile_id, offer_id, updated_at);
