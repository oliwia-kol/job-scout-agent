CREATE TABLE IF NOT EXISTS cv_documents (
    document_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    parent_document_id TEXT REFERENCES cv_documents(document_id),
    kind TEXT NOT NULL CHECK (kind IN ('master', 'derived')),
    language TEXT NOT NULL DEFAULT 'en',
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    safe_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'mapped', 'superseded')),
    section_map_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cv_documents_one_master
ON cv_documents(profile_id) WHERE kind = 'master' AND status != 'superseded';

CREATE TABLE IF NOT EXISTS cv_sections (
    section_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES cv_documents(document_id),
    section_kind TEXT NOT NULL,
    heading TEXT NOT NULL,
    anchor TEXT NOT NULL,
    block_index INTEGER NOT NULL,
    current_html TEXT NOT NULL,
    current_text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    editable INTEGER NOT NULL CHECK (editable IN (0, 1)),
    protected_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, anchor)
);

CREATE INDEX IF NOT EXISTS idx_cv_sections_document
ON cv_sections(document_id, section_kind, block_index);

CREATE TABLE IF NOT EXISTS cv_tailoring_sessions (
    session_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    profile_version INTEGER NOT NULL,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    offer_content_sha256 TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES cv_documents(document_id),
    prompt_version TEXT NOT NULL,
    model_config_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'generating', 'review', 'ready', 'packaged', 'failed', 'stale')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cv_tailoring_offer
ON cv_tailoring_sessions(offer_id, updated_at);

CREATE TABLE IF NOT EXISTS cv_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES cv_tailoring_sessions(session_id),
    section_id TEXT NOT NULL REFERENCES cv_sections(section_id),
    anchor_sha256 TEXT NOT NULL,
    current_html TEXT NOT NULL,
    current_text TEXT NOT NULL,
    proposed_html TEXT NOT NULL,
    edited_html TEXT,
    intent TEXT NOT NULL,
    profile_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    offer_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    risk_note TEXT,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'stale')),
    validation_status TEXT NOT NULL DEFAULT 'valid'
        CHECK (validation_status IN ('valid', 'invalid')),
    validation_error TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cv_suggestions_session
ON cv_suggestions(session_id, status);

CREATE TABLE IF NOT EXISTS application_packages (
    package_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES cv_tailoring_sessions(session_id),
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    html_path TEXT NOT NULL,
    html_sha256 TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    note_path TEXT NOT NULL,
    note_sha256 TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    validation_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'invalid', 'superseded')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_application_packages_offer
ON application_packages(offer_id, created_at);

CREATE TABLE IF NOT EXISTS application_events (
    event_id TEXT PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    session_id TEXT REFERENCES cv_tailoring_sessions(session_id),
    package_id TEXT REFERENCES application_packages(package_id),
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_application_events_offer
ON application_events(offer_id, created_at);
