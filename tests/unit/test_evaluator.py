from pathlib import Path

import pytest

from job_scout.demo_data import load_candidate_profile, load_frozen_demo_offers
from job_scout.domain import EvaluationDraft, ExtractedJobDetails, ScoreEvidence
from job_scout.local_llm import StructuredLlmResponse
from job_scout.model_tasks import (
    LocalModelEvaluator,
    closest_source_quote,
    profile_for_model,
    validate_evaluation_evidence,
)
from job_scout.prompts import EVALUATOR_PROMPT_VERSION

ROOT = Path(__file__).parents[2]


def draft(**overrides):
    values = {
        "opportunity_score": 8,
        "cv_fit_score": 7,
        "work_conditions_score": 9,
        "confidence": 0.8,
        "applied_ai_score": 9,
        "ai_automation_score": 7,
        "ai_evaluation_score": 6,
        "data_ml_score": 5,
        "ai_research_score": 3,
        "recommendation": "apply",
    }
    values.update(overrides)
    return EvaluationDraft(**values)


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.request = None

    async def structured_completion(self, **kwargs):
        self.request = kwargs
        return StructuredLlmResponse(
            value=self.result,
            raw_text=self.result.model_dump_json(),
            elapsed_ms=10,
            retries=0,
        )


@pytest.mark.asyncio
async def test_evaluator_uses_approved_profile_and_does_not_request_final_score():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    profile = load_candidate_profile(ROOT / "config/demo/candidate-profile-v2.json")
    result = draft(
        evidence=[
            ScoreEvidence(
                criterion="AI duties",
                quote="Support the development of scalable AI systems",
                source_field="offer.analysis_text",
            )
        ]
    )
    client = FakeClient(result)
    response = await LocalModelEvaluator(client, "qwen.gguf").evaluate(
        offer,
        ExtractedJobDetails(responsibilities=["Build AI products"], required_skills=["Python"]),
        profile,
    )
    assert response.value.opportunity_score == 8
    assert client.request["schema_name"] == EVALUATOR_PROMPT_VERSION
    assert "Do not calculate final_score" in client.request["system_prompt"]
    assert "final_score" not in client.request["schema"].model_fields
    assert "extracted_details" not in client.request["user_prompt"]


def test_evidence_must_be_exactly_grounded_in_offer_or_profile():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    profile = load_candidate_profile(ROOT / "config/demo/candidate-profile-v2.json")
    valid = draft(
        evidence=[
            ScoreEvidence(
                criterion="Python evidence",
                quote="Validated machine-learning and generative-AI systems",
                source_field="profile:synthetic-validation",
            )
        ]
    )
    validate_evaluation_evidence(offer, profile, valid)

    invalid = valid.model_copy(
        update={
            "evidence": [
                ScoreEvidence(
                    criterion="invented",
                    quote="Managed a team of 50 engineers",
                    source_field="profile:synthetic-validation",
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="not present"):
        validate_evaluation_evidence(offer, profile, invalid)


def test_model_profile_excludes_synthetic_provenance_metadata():
    profile = load_candidate_profile(ROOT / "config/demo/dummy-profile-v1.json")
    payload = profile_for_model(profile)
    assert "approved_at" not in payload
    assert "source" not in payload["evidence"][0]
    assert payload["evidence"][0]["id"] == "synthetic-llm-products"


def test_near_verbatim_quote_is_repaired_but_invention_is_rejected():
    source = "Experience in improvement and evaluation of RAG systems."
    assert (
        closest_source_quote("Expertise in improvement and evaluation of RAG systems", source)
        == "Experience in improvement and evaluation of RAG systems."
    )
    assert closest_source_quote("Managed fifty engineers in production", source) is None
