from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from job_scout.ats_scrapers import CleanJob
from job_scout.demo_flow import run_demo_flow_db
from job_scout.domain import (
    CandidateEvidence,
    CandidateProfile,
    EvaluationDraft,
    EvaluationRunItem,
    ExtractedJobDetails,
    JudgeResult,
    PipelineRun,
)
from job_scout.settings import Settings
from job_scout.storage import (
    connect,
    create_evaluation_run,
    initialize_database,
    list_evaluation_run_items,
    persist_clean_offer,
)


def _clean_offer(**overrides):
    values = {
        "source_id": "example",
        "company": "Example AI",
        "external_id": "job-42",
        "title": "AI Engineer",
        "url": "https://example.com/jobs/42",
        "locations": ["Warsaw, Poland"],
        "description": "Build AI products in Poland.",
        "analysis_text": "Build AI products in Poland.",
        "raw_sha256": "a" * 64,
        "raw_payload": "<html>first version</html>",
        "extraction_method": "html",
    }
    values.update(overrides)
    return CleanJob(**values)

def _candidate_profile():
    return CandidateProfile(
        profile_id="synthetic-flow-test",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[CandidateEvidence(id="python", statement="Uses Python", source="cv")],
        location_rule="Remote from Poland",
        approved_at=datetime.now(UTC),
    )

@pytest.fixture
def setup_run(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)

    offer = _clean_offer()
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, offer, source_url="https://example.com/careers"
        )

    profile = _candidate_profile()
    run_id = "test-run"

    def create_run(run_type):
        item = EvaluationRunItem(
            item_id=f"{run_id}:{offer_id}",
            run_id=run_id,
            offer_id=offer_id,
            status="pending",
            input_sha256="f" * 64,
            input_snapshot={"company": offer.company, "title": offer.title},
        )
        run = PipelineRun(
            run_id=run_id,
            source_id="test",
            run_type=run_type,
            status="running",
            total_items=1,
            completed_items=0,
            current_stage="starting",
            profile_id=profile.profile_id,
            profile_version=profile.version,
            model_configurations={"model": "fake-model"},
        )
        create_evaluation_run(path, run, profile, [item])
        return path, run_id, offer, profile

    return create_run

@patch("job_scout.demo_flow.LlamaServerManager")
@patch("job_scout.demo_flow.LocalLlmClient")
@patch("job_scout.demo_flow.LocalModelExtractor")
@patch("job_scout.demo_flow.LocalModelEvaluator")
@patch("job_scout.demo_flow.LocalModelJudge")
@pytest.mark.asyncio
async def test_frozen_evaluation_does_not_call_extractor(
    MockJudge, MockEvaluator, MockExtractor, MockLlmClient, MockServer, setup_run
):
    path, run_id, offer, profile = setup_run("frozen_evaluation")
    settings = Settings(local_llm_model="fake-model")

    # Provide a fake frozen extraction
    extractions = {
        (offer.company, offer.title): ExtractedJobDetails(
            responsibilities=["Code"],
            required_skills=["Python"],
            preferred_skills=[],
            seniority="Extracted AI Engineer",
        )
    }

    mock_evaluator_instance = MockEvaluator.return_value
    mock_evaluation_result = AsyncMock()
    mock_evaluation_result.value = EvaluationDraft(
        match_score=80,
        decision="yes",
        reasoning="Because",
        missing_requirements=[],
        location_status="ok",
        salary_status="ok",
        notes="",
        opportunity_score=10,
        cv_fit_score=10,
        work_conditions_score=10,
        confidence=1,
        applied_ai_score=10,
        ai_automation_score=10,
        ai_evaluation_score=10,
        data_ml_score=10,
        ai_research_score=10,
        recommendation="yes",
    )
    mock_evaluation_result.elapsed_ms = 100
    mock_evaluation_result.retries = 0
    mock_evaluator_instance.evaluate = AsyncMock(return_value=mock_evaluation_result)
    mock_judge_instance = MockJudge.return_value
    mock_judge_result = AsyncMock()
    mock_judge_result.value = JudgeResult(
        approved=True,
        confidence=1,
        reasoning="Good",
        corrected_assessment=None
    )
    mock_judge_result.elapsed_ms = 100
    mock_judge_result.retries = 0
    mock_judge_instance.review = AsyncMock(return_value=mock_judge_result)

    await run_demo_flow_db(path, run_id, [offer], profile, extractions, settings)

    MockExtractor.assert_not_called()
    assert mock_evaluator_instance.evaluate.called

@patch("job_scout.demo_flow.LlamaServerManager")
@patch("job_scout.demo_flow.LocalLlmClient")
@patch("job_scout.demo_flow.LocalModelExtractor")
@patch("job_scout.demo_flow.LocalModelEvaluator")
@patch("job_scout.demo_flow.LocalModelJudge")
@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", ["frozen_full_pipeline", "single_offer_eval"])
async def test_live_pipeline_modes_call_extractor(
    MockJudge,
    MockEvaluator,
    MockExtractor,
    MockLlmClient,
    MockServer,
    setup_run,
    run_type,
):
    path, run_id, offer, profile = setup_run(run_type)
    settings = Settings(local_llm_model="fake-model")

    mock_extractor_instance = MockExtractor.return_value
    mock_extraction_result = AsyncMock()
    mock_extraction_result.value = ExtractedJobDetails(
            responsibilities=["Code"],
            required_skills=["Python"],
            preferred_skills=[],
            seniority="Extracted AI Engineer",
    )
    mock_extraction_result.elapsed_ms = 100
    mock_extraction_result.retries = 0
    mock_extractor_instance.extract = AsyncMock(return_value=mock_extraction_result)

    mock_evaluator_instance = MockEvaluator.return_value
    mock_evaluation_result = AsyncMock()
    mock_evaluation_result.value = EvaluationDraft(
        match_score=80,
        decision="yes",
        reasoning="Because",
        missing_requirements=[],
        location_status="ok",
        salary_status="ok",
        notes="",
        opportunity_score=10,
        cv_fit_score=10,
        work_conditions_score=10,
        confidence=1,
        applied_ai_score=10,
        ai_automation_score=10,
        ai_evaluation_score=10,
        data_ml_score=10,
        ai_research_score=10,
        recommendation="yes",
    )
    mock_evaluation_result.elapsed_ms = 100
    mock_evaluation_result.retries = 0
    mock_evaluator_instance.evaluate = AsyncMock(return_value=mock_evaluation_result)
    mock_judge_instance = MockJudge.return_value
    mock_judge_result = AsyncMock()
    mock_judge_result.value = JudgeResult(
        approved=True,
        confidence=1,
        reasoning="Good",
        corrected_assessment=None
    )
    mock_judge_result.elapsed_ms = 100
    mock_judge_result.retries = 0
    mock_judge_instance.review = AsyncMock(return_value=mock_judge_result)

    await run_demo_flow_db(path, run_id, [offer], profile, {}, settings)

    mock_extractor_instance.extract.assert_called_once_with(offer)
    assert mock_evaluator_instance.evaluate.called

    items = list_evaluation_run_items(path, run_id)
    assert items[0]["extracted"]["seniority"] == "Extracted AI Engineer"
