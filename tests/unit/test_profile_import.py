from datetime import UTC, datetime

from job_scout.domain import CandidateEvidence, CandidateProfile
from job_scout.profile_import import import_curated_career_profile
from job_scout.storage import connect, get_user_profile


def _profile(*, approved: bool = True) -> CandidateProfile:
    return CandidateProfile(
        profile_id="career-profile",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[
            CandidateEvidence(
                id="local-ai",
                statement="Built a local AI evaluation prototype.",
                source="Career Knowledge Base [U]",
            )
        ],
        location_rule="Remote from Poland",
        approved_at=datetime.now(UTC) if approved else None,
    )


def test_import_approves_only_curated_profile_and_keeps_markdown_as_draft(tmp_path):
    path = tmp_path / "job_scout.db"
    profile = _profile()

    version = import_curated_career_profile(
        path,
        display_name="Career profile",
        profile=profile,
        source_markdown="# Career KB\n\n[H] Hypothesis still needs review.",
        source_filename="career.md",
    )
    repeated = import_curated_career_profile(
        path,
        display_name="Career profile",
        profile=profile,
        source_markdown="# Career KB\n\n[H] Hypothesis still needs review.",
        source_filename="career.md",
    )

    stored = get_user_profile(path, "career-profile")
    with connect(path) as connection:
        knowledge = connection.execute(
            """
            SELECT status, content_json FROM career_knowledge_base_versions
            WHERE profile_id = 'career-profile'
            """
        ).fetchall()

    assert version == repeated == 1
    assert stored["status"] == "ready"
    assert stored["profile"]["approved_at"] is not None
    assert len(knowledge) == 1
    assert knowledge[0]["status"] == "draft"
    assert '"full_markdown": "draft_requires_section_review"' in knowledge[0]["content_json"]


def test_import_rejects_unapproved_profile(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="explicitly approved"):
        import_curated_career_profile(
            tmp_path / "job_scout.db",
            display_name="Draft",
            profile=_profile(approved=False),
            source_markdown="# Draft",
            source_filename="draft.md",
        )
