from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from job_scout.domain import (
    CandidateEvidence,
    CandidateProfile,
    EvaluationDraft,
    FitAssessment,
    LocationEligibility,
    PipelineRun,
    calculate_final_score,
)


def assessment(**overrides):
    values = {
        "opportunity_score": 8,
        "cv_fit_score": 6,
        "work_conditions_score": 9,
        "final_score": 8,
        "confidence": 0.9,
        "location_eligibility": LocationEligibility.ELIGIBLE,
        "applied_ai_score": 9,
        "ai_automation_score": 8,
        "ai_evaluation_score": 5,
        "data_ml_score": 4,
        "ai_research_score": 2,
        "recommendation": "apply",
    }
    values.update(overrides)
    return FitAssessment(**values)


def test_alert_requires_score_location_and_confidence():
    assert assessment().can_alert is True
    assert assessment(confidence=0.79).can_alert is False
    assert assessment(final_score=7.9).can_alert is False
    assert assessment(location_eligibility=LocationEligibility.UNKNOWN).can_alert is False


def test_ineligible_offer_cannot_receive_alerting_score():
    with pytest.raises(ValidationError):
        assessment(location_eligibility=LocationEligibility.INELIGIBLE)


def test_demo_final_score_uses_fixed_weights():
    assert calculate_final_score(9, 6, 8) == 7.9


def test_four_dimension_score_uses_explicit_growth_weight():
    assert calculate_final_score(8, 6, 10, 9) == 8.1


def test_evaluation_draft_cannot_choose_its_own_final_score():
    draft = EvaluationDraft(
        opportunity_score=9,
        cv_fit_score=6,
        work_conditions_score=8,
        confidence=0.9,
        applied_ai_score=9,
        ai_automation_score=8,
        ai_evaluation_score=7,
        data_ml_score=5,
        ai_research_score=4,
        recommendation="apply",
    )
    assert draft.to_assessment(LocationEligibility.ELIGIBLE).final_score == 7.9


def test_candidate_profile_requires_unique_evidence_ids_and_explicit_approval():
    profile = CandidateProfile(
        profile_id="synthetic-candidate-v2",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[
            CandidateEvidence(id="model-validation", statement="Validated models", source="cv")
        ],
        location_rule="Remote from Poland or Warsaw area",
    )
    assert profile.approved is False
    assert profile.model_copy(update={"approved_at": datetime.now(UTC)}).approved is True

    with pytest.raises(ValidationError):
        CandidateProfile(
            profile_id="duplicate-evidence",
            version=1,
            target_roles=["Applied AI Engineer"],
            evidence=[profile.evidence[0], profile.evidence[0]],
            location_rule="Remote from Poland or Warsaw area",
        )


def test_pipeline_run_rejects_impossible_progress():
    with pytest.raises(ValidationError):
        PipelineRun(run_id="run-1", source_id="demo", total_items=10, completed_items=11)
