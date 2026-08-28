ALTER TABLE pipeline_runs ADD COLUMN parent_run_id TEXT REFERENCES pipeline_runs(run_id);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent_run_id
ON pipeline_runs(parent_run_id);
