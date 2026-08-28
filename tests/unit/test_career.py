from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_scout.career import (
    InterviewTurn,
    KnowledgeProposal,
    _validate_polish_turn,
    add_message,
    approve_knowledge_base_version,
    build_interview_context,
    create_knowledge_base_version,
    get_interview,
    get_latest_knowledge_base,
    persist_interview_turn,
    review_knowledge_entry,
    start_interview,
)
from job_scout.storage import (
    approve_profile_document,
    connect,
    save_profile_document,
    save_user_profile,
)


def ready_profile(path, tmp_path, profile_id="career-profile"):
    save_user_profile(
        path,
        profile_id=profile_id,
        display_name="Career Profile",
        profile_json={
            "profile_id": profile_id,
            "version": 1,
            "target_roles": ["Applied AI Engineer"],
            "evidence": [
                {"id": "automation", "statement": "Built an automation", "source": "CV"}
            ],
            "transferable_skills": [],
            "work_preferences": [],
            "negative_criteria": [],
            "location_rule": "Remote from Poland",
            "approved_at": None,
        },
        status="cv_review",
    )
    save_profile_document(
        path,
        document_id=f"document-{profile_id}",
        profile_id=profile_id,
        original_filename="cv.pdf",
        stored_path=tmp_path / "cv.pdf",
        sha256="d" * 64,
        extraction_method="pdf_text",
        extracted_text="Built an automation for a regulated workflow.",
        page_count=1,
        page_text=["Built an automation for a regulated workflow."],
    )
    approve_profile_document(
        path,
        profile_id=profile_id,
        document_id=f"document-{profile_id}",
        corrected_text="Built an automation for a regulated workflow.",
        original_language="en",
    )


def test_interview_requires_approved_profile_and_resumes_active_session(tmp_path):
    path = tmp_path / "career.db"
    save_user_profile(
        path,
        profile_id="draft",
        display_name="Draft",
        profile_json={"version": 1},
        status="draft",
    )
    with pytest.raises(ValueError, match="approved profile"):
        start_interview(path, profile_id="draft")

    ready_profile(path, tmp_path)
    first = start_interview(path, profile_id="career-profile")
    second = start_interview(path, profile_id="career-profile")
    assert first == second


def test_interview_persists_messages_summary_provenance_and_review(tmp_path):
    path = tmp_path / "career.db"
    ready_profile(path, tmp_path)
    session_id = start_interview(path, profile_id="career-profile")
    user_message_id = add_message(
        path,
        session_id=session_id,
        role="user",
        content=(
            "Samodzielnie zaprojektowałam automatyzację i sprawdziłam wyniki z użytkownikami."
        ),
    )
    turn = InterviewTurn(
        response="To pokazuje inicjatywę, ale potrzebuję jeszcze skali i efektu.",
        stage="outside_cv",
        questions=[
            "Jaki problem rozwiązywała automatyzacja?",
            "Po czym poznałaś, że rozwiązanie działa?",
        ],
        session_summary="Użytkowniczka opisała samodzielną automatyzację i walidację.",
        knowledge_proposals=[
            KnowledgeProposal(
                category="strength",
                statement_original="Wykazuje inicjatywę w budowaniu automatyzacji.",
                provenance="hypothesis",
                evidence_quote="Samodzielnie zaprojektowałam automatyzację",
                source_message_ids=[user_message_id],
                confidence=0.72,
                behavior="Samodzielnie projektuje rozwiązanie.",
                example="Automatyzacja sprawdzona z użytkownikami.",
                employer_value="Szybciej domyka eksperyment z feedbackiem.",
                overinterpretation_risk="Skala i wynik wymagają doprecyzowania.",
            )
        ],
        open_questions=["Jaka była skala automatyzacji?"],
    )
    persist_interview_turn(path, session_id=session_id, turn=turn)

    stored = get_interview(path, session_id)
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    assert "1. Jaki problem" in stored["messages"][1]["content"]
    assert stored["session"]["summary"].startswith("Użytkowniczka")
    assert stored["entries"][0]["provenance"] == "hypothesis"
    entry_id = stored["entries"][0]["entry_id"]
    review_knowledge_entry(path, entry_id, approve=True)
    with connect(path) as connection:
        reviewed = connection.execute(
            "SELECT status, provenance FROM career_knowledge_entries WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
    assert dict(reviewed) == {"status": "approved", "provenance": "user_stated"}
    approved = build_interview_context(path, session_id)["approved_knowledge"]
    assert approved[0]["entry_id"] == entry_id
    kb_version = create_knowledge_base_version(path, "career-profile")
    approve_knowledge_base_version(path, "career-profile", kb_version)
    knowledge_base = get_latest_knowledge_base(path, "career-profile")
    assert knowledge_base["status"] == "approved"
    assert knowledge_base["content"]["verified_evidence_bank"][0]["entry_id"] == entry_id
    assert "recent_messages" not in knowledge_base["content"]


def test_proposal_cannot_cite_unknown_or_non_verbatim_source(tmp_path):
    path = tmp_path / "career.db"
    ready_profile(path, tmp_path)
    session_id = start_interview(path, profile_id="career-profile")
    message_id = add_message(
        path, session_id=session_id, role="user", content="Zbudowałam lokalny prototyp."
    )
    base = {
        "response": "Dziękuję.",
        "stage": "outside_cv",
        "questions": ["Jaki był efekt?"],
        "session_summary": "Prototyp lokalny.",
    }
    for proposal in (
        KnowledgeProposal(
            category="project",
            statement_original="Prototyp",
            provenance="user_stated",
            evidence_quote="Zbudowałam lokalny prototyp",
            source_message_ids=["missing"],
            confidence=1,
        ),
        KnowledgeProposal(
            category="project",
            statement_original="Prototyp",
            provenance="user_stated",
            evidence_quote="zupełnie inny fakt",
            source_message_ids=[message_id],
            confidence=1,
        ),
    ):
        with pytest.raises(ValueError):
            persist_interview_turn(
                path,
                session_id=session_id,
                turn=InterviewTurn(**base, knowledge_proposals=[proposal]),
            )


def test_cv_verified_proposal_must_quote_reviewed_cv(tmp_path):
    path = tmp_path / "career.db"
    ready_profile(path, tmp_path)
    session_id = start_interview(path, profile_id="career-profile")
    turn = InterviewTurn(
        response="CV potwierdza doświadczenie w automatyzacji.",
        stage="cv_audit",
        questions=["Jaki był mierzalny efekt tej automatyzacji?"],
        session_summary="CV potwierdza automatyzację regulowanego procesu.",
        knowledge_proposals=[
            KnowledgeProposal(
                category="verified_experience",
                statement_original="Zbudowała automatyzację regulowanego procesu.",
                canonical_english="Built an automation for a regulated workflow.",
                provenance="cv_verified",
                evidence_quote="Built an automation for a regulated workflow.",
                source_message_ids=["document-career-profile"],
                confidence=1,
            )
        ],
    )

    persist_interview_turn(path, session_id=session_id, turn=turn)
    stored = get_interview(path, session_id)
    assert stored["entries"][0]["provenance"] == "cv_verified"


@pytest.mark.parametrize("question_count", [0, 1, 2, 3])
def test_adaptive_round_contract_allows_at_most_three_questions(question_count):
    turn = InterviewTurn(
        response="Odnoszę się do odpowiedzi.",
        stage="working_style",
        questions=[f"Pytanie {index}?" for index in range(question_count)],
        session_summary="Zwięzłe podsumowanie.",
    )
    assert len(turn.questions) == question_count

    if question_count == 3:
        with pytest.raises(ValidationError):
            InterviewTurn(
                response="Za dużo pytań.",
                stage="working_style",
                questions=["1?", "2?", "3?", "4?"],
                session_summary="Test.",
            )


def test_interview_state_survives_new_repository_read(tmp_path):
    path = tmp_path / "career.db"
    ready_profile(path, tmp_path)
    session_id = start_interview(path, profile_id="career-profile")
    add_message(
        path,
        session_id=session_id,
        role="user",
        content=f"Zapis trwały {datetime.now(UTC).isoformat()}",
    )
    reloaded = get_interview(path, session_id)
    assert reloaded["session"]["status"] == "active"
    assert len(reloaded["messages"]) == 1


def test_context_ranks_approved_knowledge_by_recent_conversation(tmp_path):
    path = tmp_path / "career.db"
    ready_profile(path, tmp_path)
    session_id = start_interview(path, profile_id="career-profile")
    message_id = add_message(
        path,
        session_id=session_id,
        role="user",
        content="Chcę wrócić do prototypu automatyzacji i jego efektu.",
    )
    for category, statement, quote in (
        (
            "work_preference",
            "Preferuje spokojne środowisko pracy.",
            "Chcę wrócić do prototypu automatyzacji i jego efektu.",
        ),
        (
            "project",
            "Zbudowała prototyp automatyzacji.",
            "Chcę wrócić do prototypu automatyzacji i jego efektu.",
        ),
    ):
        turn = InterviewTurn(
            response="Zapisuję.",
            stage="outside_cv",
            session_summary="Rozmowa o prototypie.",
            knowledge_proposals=[
                KnowledgeProposal(
                    category=category,
                    statement_original=statement,
                    provenance="user_stated",
                    evidence_quote=quote,
                    source_message_ids=[message_id],
                    confidence=1,
                )
            ],
        )
        persist_interview_turn(path, session_id=session_id, turn=turn)
        entry_id = get_interview(path, session_id)["entries"][-1]["entry_id"]
        review_knowledge_entry(path, entry_id, approve=True)

    context = build_interview_context(path, session_id)
    assert context["approved_knowledge"][0]["category"] == "project"


def test_polish_interview_validator_rejects_english_user_facing_turn():
    english = InterviewTurn(
        response="Your experience is relevant and we can discuss it.",
        stage="cv_audit",
        questions=["What was the outcome?", "How did you measure it?"],
        session_summary="The candidate described an automation project.",
    )
    with pytest.raises(ValueError, match="must be in Polish"):
        _validate_polish_turn(english)

    polish = InterviewTurn(
        response="Dziękuję, ten przykład pokazuje inicjatywę i warto go pogłębić.",
        stage="cv_audit",
        questions=["Jaki był efekt?", "Jak go zmierzyłaś?"],
        session_summary="Użytkowniczka opisała projekt automatyzacji.",
    )
    _validate_polish_turn(polish)
