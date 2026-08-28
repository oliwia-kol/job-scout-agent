from datetime import UTC, datetime

import pytest

from job_scout.ats_scrapers import CleanJob
from job_scout.domain import CandidateEvidence, CandidateProfile
from job_scout.offer_copilot import (
    OfferCitation,
    OfferCopilotTurn,
    ProfileUpdateProposal,
    add_offer_chat_message,
    build_offer_chat_context,
    get_offer_chat,
    persist_offer_copilot_turn,
    start_offer_chat,
    validate_offer_copilot_turn,
)
from job_scout.storage import (
    connect,
    initialize_database,
    persist_clean_offer,
    review_profile_fact,
    save_profile_fact,
    save_user_profile,
)


def _approve_scoring_facts(path, profile_id):
    for category, value in (
        ("target_role", "Applied AI Engineer"),
        ("work_location", "Poland"),
        ("work_model", "Remote"),
        ("contract", "No constraint"),
        ("language", "English B2"),
        ("travel", "No travel"),
        ("experience", "Built Python automation with measurable business impact."),
    ):
        fact_id = save_profile_fact(
            path,
            profile_id=profile_id,
            category=category,
            value={"text": value},
            source_type="user_message",
            source_ref="test-message",
            source_quote=value,
            usable_for_scoring=True,
        )
        review_profile_fact(path, fact_id=fact_id, approve=True)


def setup_chat(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    profile = CandidateProfile(
        profile_id="profile-a",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[
            CandidateEvidence(
                id="python",
                statement="Built Python automation with measurable business impact.",
                source="test",
            )
        ],
        location_rule="Remote from Poland",
        approved_at=datetime.now(UTC),
    )
    save_user_profile(
        path,
        profile_id=profile.profile_id,
        display_name="Profile A",
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    _approve_scoring_facts(path, profile.profile_id)
    offer = CleanJob(
        source_id="test",
        company="Example",
        external_id="1",
        title="Applied AI Engineer",
        url="https://example.com/jobs/1",
        description="Build RAG systems in Python. Remote work from Poland.",
        analysis_text="Build RAG systems in Python. Remote work from Poland.",
        raw_sha256="a" * 64,
        extraction_method="test",
    )
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, offer, source_url="https://example.com/careers"
        )
    session_id = start_offer_chat(path, profile_id=profile.profile_id, offer_id=offer_id)
    add_offer_chat_message(
        path,
        session_id=session_id,
        role="user",
        content="Czy warto aplikować?",
    )
    return path, session_id


def test_offer_copilot_persists_grounded_answer_and_proposal(tmp_path):
    path, session_id = setup_chat(tmp_path)
    turn = OfferCopilotTurn(
        response_pl="Ta rola jest zgodna z Twoim kierunkiem i warto ją rozważyć.",
        citations=[
            OfferCitation(
                source_field="offer.analysis_text",
                quote="Build RAG systems in Python.",
            )
        ],
        profile_update_proposals=[
            ProfileUpdateProposal(
                statement="Evidence of Python automation.",
                source_field="profile:python",
                evidence_quote="Built Python automation",
            )
        ],
    )
    persist_offer_copilot_turn(path, session_id=session_id, turn=turn)
    chat = get_offer_chat(path, session_id)
    assert [message["role"] for message in chat["messages"]] == ["user", "assistant"]
    assert chat["proposals"][0]["status"] == "proposed"


def test_offer_copilot_rejects_ungrounded_citation(tmp_path):
    path, session_id = setup_chat(tmp_path)
    context = build_offer_chat_context(path, session_id)
    turn = OfferCopilotTurn(
        response_pl="Ta oferta jest ciekawa, ale wymaga sprawdzenia.",
        citations=[
            OfferCitation(
                source_field="offer.analysis_text",
                quote="Five years of Kubernetes",
            )
        ],
    )
    with pytest.raises(ValueError, match="exact source quote"):
        validate_offer_copilot_turn(context, turn)


def test_offer_copilot_context_includes_persisted_assessment(tmp_path):
    path, session_id = setup_chat(tmp_path)
    with connect(path) as connection:
        connection.execute(
            "UPDATE offers SET assessment_json = ? WHERE id = 1",
            ('{"final_score": 8.4, "recommendation": "apply"}',),
        )

    context = build_offer_chat_context(path, session_id)

    assert '"final_score": 8.4' in context["sources"]["offer.assessment"]
    assert '"recommendation": "apply"' in context["sources"]["offer.assessment"]
