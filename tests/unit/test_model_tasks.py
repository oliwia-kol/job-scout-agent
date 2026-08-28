from pathlib import Path

import pytest

from job_scout.demo_data import load_candidate_profile, load_frozen_demo_offers
from job_scout.domain import EvaluationDraft, ExtractedJobDetails, ScoreEvidence
from job_scout.local_llm import StructuredLlmResponse
from job_scout.model_tasks import (
    LocalModelExtractor,
    enrich_with_deterministic_facts,
    validate_evaluation_evidence,
    validate_extraction_completeness,
    validate_model_extraction,
)
from job_scout.prompts import EXTRACTOR_PROMPT_VERSION

ROOT = Path(__file__).parents[2]


class FakeClient:
    def __init__(self):
        self.request = None

    async def structured_completion(self, **kwargs):
        self.request = kwargs
        value = ExtractedJobDetails(
            responsibilities=["Build AI products"],
            required_skills=["Python"],
        )
        return StructuredLlmResponse(
            value=value,
            raw_text=value.model_dump_json(),
            elapsed_ms=10,
            retries=0,
        )


@pytest.mark.asyncio
async def test_extractor_uses_clean_model_text_and_versioned_schema():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    client = FakeClient()
    result = await LocalModelExtractor(client, "qwen.gguf").extract(offer)
    assert result.value.required_skills == ["Python"]
    assert client.request["schema"] is ExtractedJobDetails
    assert client.request["schema_name"] == EXTRACTOR_PROMPT_VERSION
    assert offer.analysis_text in client.request["user_prompt"]
    assert str(offer.url) not in client.request["user_prompt"]
    assert "&lt;li&gt;" not in client.request["user_prompt"]
    assert "Map every distinct duty" in client.request["user_prompt"]
    assert "both responsibilities and required_skills must contain" in client.request["user_prompt"]


def test_completeness_check_rejects_omitted_explicit_compensation():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    with pytest.raises(ValueError, match="salary.*contract_type"):
        validate_extraction_completeness(
            offer,
            ExtractedJobDetails(work_mode="Remote"),
        )


def test_explicit_supplemental_facts_fill_model_omissions():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    enriched = enrich_with_deterministic_facts(offer, ExtractedJobDetails(work_mode="Remote"))
    assert enriched.salary and "PLN" in enriched.salary
    assert enriched.contract_type == "B2B"
    validate_extraction_completeness(offer, enriched)


def test_model_validation_requires_both_labelled_sections():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    with pytest.raises(ValueError, match="responsibilities.*required_skills"):
        validate_model_extraction(offer, ExtractedJobDetails())


@pytest.mark.parametrize(
    "incorrect_source",
    ["profile:synthetic-flow-test:negative_criteria", "profile:synthetic-llm-products"],
)
def test_evidence_validation_repairs_unique_exact_source(incorrect_source):
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    profile = load_candidate_profile(ROOT / "config/demo/dummy-profile-v1.json")
    draft = EvaluationDraft(
        opportunity_score=5,
        cv_fit_score=5,
        work_conditions_score=5,
        confidence=0.5,
        applied_ai_score=5,
        ai_automation_score=5,
        ai_evaluation_score=5,
        data_ml_score=5,
        ai_research_score=5,
        evidence=[
            ScoreEvidence(
                criterion="work preference",
                quote="intensive presales",
                source_field=incorrect_source,
            )
        ],
        recommendation="consider",
    )

    validate_evaluation_evidence(offer, profile, draft)

    assert draft.evidence[0].source_field == "profile:negative_criteria"
