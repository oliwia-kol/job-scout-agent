from job_scout.domain import EvaluationDraft
from job_scout.language_benchmark import _draft_metrics


def test_language_benchmark_calculates_application_owned_final_score():
    draft = EvaluationDraft(
        opportunity_score=8,
        cv_fit_score=6,
        work_conditions_score=10,
        confidence=0.8,
        applied_ai_score=8,
        ai_automation_score=7,
        ai_evaluation_score=6,
        data_ml_score=5,
        ai_research_score=4,
        evidence=[],
        recommendation="apply",
    )
    metrics = _draft_metrics(draft, elapsed_ms=1200, retries=0)
    assert metrics["final_score"] == 7.8
