"""Safe local import of an already prepared Career Knowledge Base."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from .domain import CandidateProfile
from .storage import connect, initialize_database, save_user_profile


def import_curated_career_profile(
    database_path: Path,
    *,
    display_name: str,
    profile: CandidateProfile,
    source_markdown: str,
    source_filename: str,
) -> int:
    """Import a reviewed scoring profile and keep the full narrative as a draft.

    The curated ``CandidateProfile`` may be used for scoring. The larger Markdown
    document remains a draft because it can contain hypotheses, private notes and
    open questions that must not silently become candidate evidence.
    """
    if not profile.approved:
        raise ValueError("curated profile must be explicitly approved")
    markdown = source_markdown.strip()
    if not markdown:
        raise ValueError("career knowledge base markdown cannot be empty")
    initialize_database(database_path)
    save_user_profile(
        database_path,
        profile_id=profile.profile_id,
        display_name=display_name.strip(),
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    timestamp = datetime.now(UTC).isoformat()
    source_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    with connect(database_path) as connection:
        existing = connection.execute(
            """
            SELECT version FROM career_knowledge_base_versions
            WHERE profile_id = ?
              AND json_extract(content_json, '$.source.sha256') = ?
            ORDER BY version DESC LIMIT 1
            """,
            (profile.profile_id, source_sha256),
        ).fetchone()
        if existing:
            return int(existing["version"])
        version = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM career_knowledge_base_versions WHERE profile_id = ?
                """,
                (profile.profile_id,),
            ).fetchone()[0]
        )
        content = {
            "schema_version": "career-kb-markdown-v1",
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "profile_snapshot": profile.model_dump(mode="json"),
            "source": {
                "filename": source_filename,
                "sha256": source_sha256,
                "original_language": "pl",
                "imported_at": timestamp,
            },
            "approval_scope": {
                "scoring_profile": "approved_curated_facts",
                "full_markdown": "draft_requires_section_review",
            },
            "markdown": markdown,
        }
        connection.execute(
            """
            INSERT INTO career_knowledge_base_versions (
                profile_id, version, content_json, status, created_at
            ) VALUES (?, ?, ?, 'draft', ?)
            """,
            (
                profile.profile_id,
                version,
                json.dumps(content, ensure_ascii=False, sort_keys=True),
                timestamp,
            ),
        )
    return version
