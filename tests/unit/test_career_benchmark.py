from job_scout.career import InterviewTurn, KnowledgeProposal
from job_scout.career_benchmark import (
    SCENARIOS,
    _acceptance_checks,
    _scenario_context,
)


def test_career_benchmark_has_five_distinct_scenarios():
    assert len(SCENARIOS) == 5
    assert len({scenario.scenario_id for scenario in SCENARIOS}) == 5


def test_acceptance_checks_validate_adaptivity_language_and_grounding():
    scenario = SCENARIOS[1]
    context = _scenario_context(scenario)
    turn = InterviewTurn(
        response="Twój lokalny prototyp jest ważnym dowodem inicjatywy.",
        stage="outside_cv",
        questions=["Jaki był efekt prototypu?"],
        session_summary="Użytkowniczka zbudowała prototyp; efekt wymaga doprecyzowania.",
        knowledge_proposals=[
            KnowledgeProposal(
                category="project",
                statement_original="Zbudowała lokalny prototyp.",
                provenance="user_stated",
                evidence_quote="zbudowałam lokalny prototyp",
                source_message_ids=["user-follow-up-depth"],
                confidence=1,
            )
        ],
    )

    checks = _acceptance_checks(turn, context, scenario.expected_terms)

    assert all(checks.values())


def test_acceptance_checks_reject_ungrounded_quote():
    scenario = SCENARIOS[0]
    context = _scenario_context(scenario)
    turn = InterviewTurn(
        response="CV pokazuje automatyzację.",
        stage="cv_audit",
        questions=["Jaki był efekt automatyzacji?"],
        session_summary="Audyt CV.",
        knowledge_proposals=[
            KnowledgeProposal(
                category="cv",
                statement_original="Niepotwierdzony wynik.",
                provenance="cv_verified",
                evidence_quote="Reduced costs by 90%.",
                source_message_ids=["cv-cv-audit"],
                confidence=1,
            )
        ],
    )

    assert not _acceptance_checks(turn, context, scenario.expected_terms)[
        "grounded_proposals"
    ]
