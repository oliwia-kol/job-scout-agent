CREATE TABLE IF NOT EXISTS evaluation_feedback (
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    evaluation_input_sha256 TEXT NOT NULL,
    offer_content_sha256 TEXT NOT NULL,
    feedback_type TEXT NOT NULL CHECK (feedback_type = 'false_negative'),
    note TEXT NOT NULL DEFAULT '',
    model_recommendation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (offer_id, evaluation_input_sha256)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_feedback_type
ON evaluation_feedback(feedback_type, updated_at);
