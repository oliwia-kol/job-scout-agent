CREATE TABLE IF NOT EXISTS profile_facts (
    fact_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    category TEXT NOT NULL,
    value_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'rejected', 'superseded')),
    source_type TEXT NOT NULL
        CHECK (source_type IN ('cv', 'user_message', 'career_base')),
    source_ref TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    priority TEXT CHECK (priority IN ('preference', 'important', 'required')),
    usable_for_scoring INTEGER NOT NULL DEFAULT 0 CHECK (usable_for_scoring IN (0, 1)),
    usable_for_cv INTEGER NOT NULL DEFAULT 0 CHECK (usable_for_cv IN (0, 1)),
    profile_version INTEGER,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_facts_profile_status
ON profile_facts(profile_id, status, category);

CREATE TABLE IF NOT EXISTS profile_fact_conflicts (
    conflict_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    category TEXT NOT NULL,
    fact_id_a TEXT NOT NULL REFERENCES profile_facts(fact_id),
    fact_id_b TEXT NOT NULL REFERENCES profile_facts(fact_id),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(profile_id, category, fact_id_a, fact_id_b)
);

CREATE INDEX IF NOT EXISTS idx_profile_fact_conflicts_open
ON profile_fact_conflicts(profile_id, status);
