ALTER TABLE offers
ADD COLUMN original_language TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE offers
ADD COLUMN language_confidence REAL;

ALTER TABLE offers
ADD COLUMN language_detected_at TEXT;

CREATE TABLE IF NOT EXISTS offer_translations (
    translation_id TEXT PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    source_content_sha256 TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL DEFAULT 'en',
    translated_json TEXT NOT NULL,
    quote_map_json TEXT NOT NULL,
    translator_model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE(offer_id, source_content_sha256, target_language, translator_model, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_offer_translations_lookup
ON offer_translations(offer_id, target_language, status, created_at);
