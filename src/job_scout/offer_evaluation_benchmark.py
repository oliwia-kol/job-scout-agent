"""Live quality and latency benchmark for Bielik on stored job offers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob
from .domain import (
    CandidateProfile,
    EvaluationDraft,
    ExtractedJobDetails,
    ScoreEvidence,
)
from .local_llm import LocalLlmClient
from .model_tasks import (
    enrich_with_deterministic_facts,
    profile_for_model,
    validate_evaluation_evidence,
)
from .prefilter import infer_location
from .prompts import (
    EVALUATOR_INSTRUCTION,
    EVALUATOR_PROMPT_VERSION,
    EVALUATOR_SYSTEM_PROMPT,
)
from .storage import get_offer


class CompactEvidenceRef(BaseModel):
    criterion: str = Field(min_length=1, max_length=80)
    evidence_id: str = Field(min_length=1, max_length=80)


class CompactBielikEvaluation(BaseModel):
    opportunity_score: float = Field(ge=0, le=10)
    cv_fit_score: float = Field(ge=0, le=10)
    work_conditions_score: float = Field(ge=0, le=10)
    development_potential_score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    applied_ai_score: float = Field(ge=0, le=10)
    ai_automation_score: float = Field(ge=0, le=10)
    ai_evaluation_score: float = Field(ge=0, le=10)
    data_ml_score: float = Field(ge=0, le=10)
    ai_research_score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list, max_length=4)
    gaps: list[str] = Field(default_factory=list, max_length=4)
    evidence: list[CompactEvidenceRef] = Field(min_length=2, max_length=6)
    recommendation: Literal["apply", "consider", "prepare_first", "low_priority"]

    def to_draft(self, evidence_catalog: dict[str, tuple[str, str]]) -> EvaluationDraft:
        payload = self.model_dump()
        payload["evidence"] = [
            ScoreEvidence(
                criterion=item.criterion,
                source_field=evidence_catalog[item.evidence_id][0],
                quote=evidence_catalog[item.evidence_id][1],
            )
            for item in self.evidence
        ]
        return EvaluationDraft.model_validate(payload)


async def run_bielik_offer_benchmark(
    database_path: Path,
    *,
    offer_ids: list[int],
    profile: CandidateProfile,
    client: LocalLlmClient,
    model: str,
) -> dict:
    """Evaluate selected stored offers without replacing production assessments."""
    results: list[dict] = []
    for offer_id in offer_ids:
        stored = get_offer(database_path, offer_id)
        if not stored:
            results.append(
                {"offer_id": offer_id, "status": "failed", "error": "offer not found"}
            )
            continue
        offer = CleanJob.model_validate(stored["offer"])
        item = {
            "offer_id": offer_id,
            "company": offer.company,
            "title": offer.title,
            "original_language": stored.get("original_language") or "unknown",
            "status": "running",
        }
        try:
            extracted = _deterministic_section_extraction(offer)
            evidence_catalog = _evidence_catalog(offer, extracted, profile)
            model_input = {
                "job": {
                    "company": offer.company,
                    "title": offer.title,
                    "locations": offer.locations,
                    "responsibilities": extracted.responsibilities,
                    "requirements": extracted.required_skills,
                    "work_mode": extracted.work_mode,
                    "salary": extracted.salary,
                    "contract_type": extracted.contract_type,
                },
                "extraction_gate": "passed",
                "candidate_profile": profile_for_model(profile),
                "evidence_catalog": [
                    {"id": key, "text": value[1]}
                    for key, value in evidence_catalog.items()
                ],
            }
            evaluation = await client.structured_completion(
                model=model,
                system_prompt=EVALUATOR_SYSTEM_PROMPT,
                user_prompt=(
                    EVALUATOR_INSTRUCTION
                    + "\nFor every evidence item return criterion and one evidence_id "
                    "copied exactly from evidence_catalog. Never rewrite evidence text."
                    + "\nUse at least one job evidence_id and at least one candidate "
                    "evidence_id."
                    + "\nUse short phrases. Keep the complete JSON below 550 tokens."
                    + "\n\nEvaluate this job and candidate profile:\n"
                    + json.dumps(
                        model_input,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                ),
                schema=CompactBielikEvaluation,
                schema_name="bielik-evaluator-compact-v1",
                strict_schema=False,
                max_tokens=650,
                temperature=0,
                validate=lambda value: _validate_compact_evaluation(
                    offer, profile, value, evidence_catalog
                ),
            )
            draft = evaluation.value.to_draft(evidence_catalog)
            assessment = draft.to_assessment(infer_location(offer))
            item.update(
                {
                    "status": "completed",
                    "extraction": extracted.model_dump(mode="json"),
                    "extraction_method": "deterministic_labelled_sections",
                    "draft": draft.model_dump(mode="json"),
                    "assessment": assessment.model_dump(mode="json"),
                    "timings_ms": {
                        "evaluator": evaluation.elapsed_ms,
                        "total": evaluation.elapsed_ms,
                    },
                    "retries": {
                        "evaluator": evaluation.retries,
                    },
                    "quality_checks": {
                        "has_offer_and_profile_evidence": _has_both_evidence_types(
                            assessment.model_dump(mode="json")
                        ),
                        "recommendation_is_allowed": assessment.recommendation
                        in {"apply", "consider", "prepare_first", "low_priority"},
                        "independent_judge_run": False,
                    },
                }
            )
        except Exception as exc:
            item.update(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        results.append(item)
    completed = [item for item in results if item["status"] == "completed"]
    return {
        "schema_version": "bielik-offer-benchmark-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "model": model,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "prompt_versions": {
            "evaluator": EVALUATOR_PROMPT_VERSION,
        },
        "pipeline": "deterministic labelled-section extraction -> Bielik evaluator",
        "total": len(results),
        "completed": len(completed),
        "failed": len(results) - len(completed),
        "mean_total_ms": (
            round(
                sum(item["timings_ms"]["total"] for item in completed) / len(completed)
            )
            if completed
            else None
        ),
        "results": results,
    }


def write_bielik_offer_benchmark(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _has_both_evidence_types(assessment: dict) -> bool:
    sources = {
        str(item.get("source_field") or "")
        for item in assessment.get("evidence") or []
    }
    return any(source.startswith("offer.") for source in sources) and any(
        source.startswith("profile:") for source in sources
    )


def _deterministic_section_extraction(offer: CleanJob) -> ExtractedJobDetails:
    responsibilities = _section_items(
        offer.analysis_text,
        start="ROLE RESPONSIBILITIES:",
        end="ROLE REQUIREMENTS:",
    )
    requirements = _section_items(
        offer.analysis_text,
        start="ROLE REQUIREMENTS:",
        end=None,
    )
    extracted = ExtractedJobDetails(
        responsibilities=responsibilities or ["See complete role text"],
        required_skills=requirements or ["See complete role text"],
    )
    return enrich_with_deterministic_facts(offer, extracted)


def _section_items(text: str, *, start: str, end: str | None) -> list[str]:
    if start not in text:
        return []
    section = text.split(start, 1)[1]
    if end and end in section:
        section = section.split(end, 1)[0]
    values = [
        " ".join(value.split())
        for value in re.split(r"(?:\s+[•·]\s+|\n+|(?<=[.!?])\s+)", section)
    ]
    return [value[:320] for value in values if len(value) >= 12][:12]


def _evidence_catalog(
    offer: CleanJob,
    extracted: ExtractedJobDetails,
    profile: CandidateProfile,
) -> dict[str, tuple[str, str]]:
    """Expose immutable evidence handles instead of asking the model to quote text."""
    catalog: dict[str, tuple[str, str]] = {}
    offer_items = [*extracted.responsibilities, *extracted.required_skills]
    for index, statement in enumerate(dict.fromkeys(offer_items)):
        if statement != "See complete role text":
            catalog[f"job-{index + 1}"] = ("offer.analysis_text", statement)

    work_items = (
        offer.supplemental_info.work_conditions
        + offer.supplemental_info.travel_requirements
    )
    for index, item in enumerate(work_items):
        catalog[f"work-{index + 1}"] = ("offer.work_conditions", item.evidence)

    for item in profile.evidence:
        catalog[item.id] = (f"profile:{item.id}", item.statement)

    profile_lists = {
        "preference": ("profile:work_preferences", profile.work_preferences),
        "negative": ("profile:negative_criteria", profile.negative_criteria),
        "transferable": (
            "profile:transferable_skills",
            profile.transferable_skills,
        ),
    }
    for prefix, (source_field, values) in profile_lists.items():
        for index, value in enumerate(values):
            catalog[f"{prefix}-{index + 1}"] = (source_field, value)
    if profile.location_rule:
        catalog["location-rule"] = ("profile:location_rule", profile.location_rule)
    return catalog


def _validate_compact_evaluation(
    offer: CleanJob,
    profile: CandidateProfile,
    value: CompactBielikEvaluation,
    evidence_catalog: dict[str, tuple[str, str]],
) -> None:
    draft = value.to_draft(evidence_catalog)
    validate_evaluation_evidence(offer, profile, draft)
    sources = {item.source_field for item in draft.evidence}
    if not any(source.startswith("offer.") for source in sources):
        raise ValueError("evaluation needs at least one job evidence_id")
    if not any(source.startswith("profile:") for source in sources):
        raise ValueError("evaluation needs at least one candidate evidence_id")
