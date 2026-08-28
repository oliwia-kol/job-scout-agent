ALTER TABLE offers ADD COLUMN current_content_sha256 TEXT;
ALTER TABLE offers ADD COLUMN evaluation_input_sha256 TEXT;
ALTER TABLE offers ADD COLUMN needs_evaluation INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS offer_versions (
    version_id TEXT PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    content_sha256 TEXT NOT NULL,
    offer_json TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    changed_fields_json TEXT NOT NULL DEFAULT '[]',
    collection_run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(offer_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS offer_events (
    event_id TEXT PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL DEFAULT '{}',
    collection_run_id TEXT,
    created_at TEXT NOT NULL,
    read_at TEXT
);

CREATE TABLE IF NOT EXISTS export_queue (
    export_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(provider, entity_type, entity_id, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_offer_versions_offer
ON offer_versions(offer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_offer_events_recent
ON offer_events(created_at, event_type, read_at);
CREATE INDEX IF NOT EXISTS idx_export_queue_status
ON export_queue(provider, status, created_at);
CREATE INDEX IF NOT EXISTS idx_offers_needs_evaluation
ON offers(needs_evaluation, availability_status, last_seen_at);
