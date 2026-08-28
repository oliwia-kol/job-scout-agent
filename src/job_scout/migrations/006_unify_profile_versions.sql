ALTER TABLE user_profiles
ADD COLUMN current_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE user_profiles
ADD COLUMN completeness_status TEXT NOT NULL DEFAULT 'profile_draft';

ALTER TABLE user_profiles
ADD COLUMN interview_language TEXT NOT NULL DEFAULT 'pl';

ALTER TABLE user_profiles
ADD COLUMN canonical_language TEXT NOT NULL DEFAULT 'en';

ALTER TABLE profile_documents
ADD COLUMN original_language TEXT NOT NULL DEFAULT 'mixed';

ALTER TABLE profile_documents
ADD COLUMN extractor_version TEXT NOT NULL DEFAULT 'pdf-ocr-v1';

ALTER TABLE profile_documents
ADD COLUMN corrected_text TEXT;

ALTER TABLE profile_documents
ADD COLUMN review_status TEXT NOT NULL DEFAULT 'needs_review';

ALTER TABLE profile_documents
ADD COLUMN reviewed_at TEXT;

ALTER TABLE profile_documents
ADD COLUMN page_text_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS profile_versions (
    profile_id TEXT NOT NULL REFERENCES user_profiles(profile_id),
    version INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    superseded_at TEXT,
    PRIMARY KEY (profile_id, version)
);

INSERT OR IGNORE INTO user_profiles (
    profile_id, display_name, profile_json, status, created_at, updated_at,
    current_version, completeness_status, interview_language, canonical_language
)
SELECT
    candidate.profile_id,
    candidate.profile_id,
    candidate.profile_json,
    CASE
        WHEN json_extract(candidate.profile_json, '$.approved_at') IS NOT NULL THEN 'ready'
        ELSE 'draft'
    END,
    candidate.created_at,
    candidate.created_at,
    candidate.version,
    CASE
        WHEN json_extract(candidate.profile_json, '$.approved_at') IS NOT NULL
            THEN 'legacy_profile_only'
        ELSE 'profile_draft'
    END,
    'pl',
    'en'
FROM candidate_profiles AS candidate
WHERE candidate.version = (
    SELECT MAX(latest.version)
    FROM candidate_profiles AS latest
    WHERE latest.profile_id = candidate.profile_id
);

INSERT OR IGNORE INTO profile_versions (
    profile_id, version, profile_json, status, source, created_at, approved_at
)
SELECT
    profile_id,
    current_version,
    profile_json,
    CASE WHEN status = 'ready' THEN 'approved' ELSE 'proposed' END,
    'user_profile_migration',
    created_at,
    json_extract(profile_json, '$.approved_at')
FROM user_profiles;

INSERT OR IGNORE INTO profile_versions (
    profile_id, version, profile_json, status, source, created_at, approved_at
)
SELECT
    profile_id,
    version,
    profile_json,
    CASE
        WHEN json_extract(profile_json, '$.approved_at') IS NOT NULL THEN 'approved'
        ELSE 'proposed'
    END,
    'candidate_profile_legacy',
    created_at,
    json_extract(profile_json, '$.approved_at')
FROM candidate_profiles;

UPDATE user_profiles
SET current_version = (
    SELECT MAX(version)
    FROM profile_versions
    WHERE profile_versions.profile_id = user_profiles.profile_id
);

UPDATE user_profiles
SET completeness_status = CASE
    WHEN status = 'ready' AND EXISTS (
        SELECT 1 FROM profile_documents
        WHERE profile_documents.profile_id = user_profiles.profile_id
    ) THEN 'cv_review'
    WHEN status = 'ready' THEN 'legacy_profile_only'
    ELSE 'profile_draft'
END;

CREATE INDEX IF NOT EXISTS idx_profile_versions_status
ON profile_versions(profile_id, status, version);

CREATE INDEX IF NOT EXISTS idx_profile_documents_review
ON profile_documents(profile_id, review_status, created_at);
