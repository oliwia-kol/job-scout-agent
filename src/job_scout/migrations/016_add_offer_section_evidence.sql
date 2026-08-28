CREATE TABLE IF NOT EXISTS offer_section_evidence (
    evidence_id TEXT PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    content_sha256 TEXT NOT NULL,
    section_kind TEXT NOT NULL,
    category TEXT,
    value_json TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    UNIQUE(offer_id, content_sha256, section_kind, category, source_quote)
);

CREATE INDEX IF NOT EXISTS idx_offer_section_evidence_current
ON offer_section_evidence(offer_id, content_sha256, section_kind);
