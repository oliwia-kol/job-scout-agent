CREATE TABLE IF NOT EXISTS candidate_profiles (
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, version)
);

CREATE TABLE IF NOT EXISTS source_diagnostics (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    offers_found INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at ON pipeline_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_evaluation_run_items_run_offer ON evaluation_run_items(run_id, offer_id);
