CREATE TABLE IF NOT EXISTS raw_offers (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    source_url TEXT NOT NULL,
    job_url TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    external_id TEXT,
    title_hint TEXT,
    payload TEXT NOT NULL,
    content_type TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE(identity_key, payload_sha256)
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    identity_key TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    job_url TEXT NOT NULL UNIQUE,
    offer_json TEXT NOT NULL,
    assessment_json TEXT,
    judge_json TEXT,
    application_status TEXT NOT NULL DEFAULT 'new',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    run_type TEXT NOT NULL DEFAULT 'collection',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    total_items INTEGER NOT NULL DEFAULT 0,
    completed_items INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT,
    profile_id TEXT,
    profile_version INTEGER,
    profile_json TEXT,
    prompt_versions_json TEXT NOT NULL DEFAULT '{}',
    mlflow_run_id TEXT,
    models_json TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS evaluation_run_items (
    item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    status TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    extracted_json TEXT,
    draft_json TEXT,
    judgment_json TEXT,
    final_assessment_json TEXT,
    timings_json TEXT NOT NULL DEFAULT '{}',
    retry_counts_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    UNIQUE(run_id, offer_id)
);

CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    action TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
