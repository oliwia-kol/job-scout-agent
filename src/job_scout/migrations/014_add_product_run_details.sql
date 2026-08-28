-- Product-facing scan telemetry.  The detailed rejection log is intentionally
-- separate from offers: rejected candidates are diagnostics, not job records.
ALTER TABLE collection_runs ADD COLUMN discovered_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE collection_runs ADD COLUMN rejected_title_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE collection_runs ADD COLUMN rejected_location_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE collection_runs ADD COLUMN processing_error_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS collection_rejections (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    stage TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_collection_rejections_run
ON collection_rejections(run_id, stage);
