from pathlib import Path

import pytest

from job_scout.demo_data import load_candidate_profile, load_frozen_demo_offers
from job_scout.domain import EvaluationDraft, JudgeResult, ScoreEvidence
from job_scout.model_tasks import normalize_judgment, validate_judgment

ROOT = Path(__file__).parents[2]


def evaluation():
    return EvaluationDraft(
        opportunity_score=8,
        cv_fit_score=8,
        work_conditions_score=8,
        confidence=0.8,
        applied_ai_score=8,
        ai_automation_score=8,
        ai_evaluation_score=8,
        data_ml_score=5,
        ai_research_score=4,
        recommendation="apply",
        evidence=[
            ScoreEvidence(
                criterion="candidate skill",
                quote="Built and deployed production LLM applications",
                source_field="profile:synthetic-llm-products",
            )
        ],
    )


def test_judge_rejects_inconsistent_verdicts():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    profile = load_candidate_profile(ROOT / "config/demo/dummy-profile-v1.json")
    with pytest.raises(ValueError, match="approved judgment"):
        validate_judgment(
            offer,
            profile,
            JudgeResult(
                approved=True,
                confidence=0.9,
                corrected_assessment=evaluation(),
            ),
        )
    with pytest.raises(ValueError, match="must explain"):
        validate_judgment(offer, profile, JudgeResult(approved=False, confidence=0.7))


def test_dummy_profile_is_clearly_synthetic_and_approved():
    profile = load_candidate_profile(ROOT / "config/demo/dummy-profile-v1.json")
    assert profile.approved
    assert profile.profile_id == "synthetic-flow-test"
    assert all("SYNTHETIC TEST DATA" in item.source for item in profile.evidence)


def test_incomplete_self_review_becomes_visible_warning():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    profile = load_candidate_profile(ROOT / "config/demo/dummy-profile-v1.json")
    normalized = normalize_judgment(offer, profile, JudgeResult(approved=False, confidence=0.5))
    assert normalized.approved is False
    assert normalized.corrected_assessment is None
    assert normalized.issues == ["Self-review rejected the draft without stating a reason"]
