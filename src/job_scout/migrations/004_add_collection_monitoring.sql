ALTER TABLE offers ADD COLUMN availability_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE offers ADD COLUMN last_checked_at TEXT;
ALTER TABLE offers ADD COLUMN disappeared_at TEXT;
ALTER TABLE offers ADD COLUMN last_collection_run_id TEXT;

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    sources_total INTEGER NOT NULL DEFAULT 0,
    sources_ok INTEGER NOT NULL DEFAULT 0,
    offers_saved INTEGER NOT NULL DEFAULT 0,
    new_raw_versions INTEGER NOT NULL DEFAULT 0,
    offers_marked_unavailable INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS collection_source_runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    selected_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    checked_at TEXT NOT NULL,
    UNIQUE(run_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_availability ON offers(availability_status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_collection_runs_started ON collection_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_collection_source_runs_source ON collection_source_runs(source_id, checked_at);
