"""Typed local-model tasks for the AI demo pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from difflib import SequenceMatcher

from .ats_scrapers import CleanJob
from .demo_data import build_model_input_snapshot
from .domain import CandidateProfile, EvaluationDraft, ExtractedJobDetails, JudgeResult
from .local_llm import LocalLlmClient, StructuredLlmResponse
from .prompts import (
    EVALUATOR_INSTRUCTION,
    EVALUATOR_PROMPT_VERSION,
    EVALUATOR_SYSTEM_PROMPT,
    EXTRACTOR_MAPPING_INSTRUCTION,
    EXTRACTOR_PROMPT_VERSION,
    EXTRACTOR_SYSTEM_PROMPT,
    JUDGE_INSTRUCTION,
    JUDGE_PROMPT_VERSION,
    JUDGE_SYSTEM_PROMPT,
)


class LocalModelExtractor:
    def __init__(self, client: LocalLlmClient, model: str) -> None:
        self.client = client
        self.model = model

    async def extract(self, offer: CleanJob) -> StructuredLlmResponse[ExtractedJobDetails]:
        snapshot = build_model_input_snapshot(offer)
        model_input = {
            "analysis_text": snapshot["analysis_text"],
            "supplemental_info": snapshot["supplemental_info"],
        }
        response = await self.client.structured_completion(
            model=self.model,
            system_prompt=EXTRACTOR_SYSTEM_PROMPT,
            user_prompt=(
                EXTRACTOR_MAPPING_INSTRUCTION
                + "\n\nExtract the structured job details from this input:\n"
                + json.dumps(model_input, ensure_ascii=False, sort_keys=True)
            ),
            schema=ExtractedJobDetails,
            schema_name=EXTRACTOR_PROMPT_VERSION,
            validate=lambda value: validate_model_extraction(offer, value),
        )
        enriched = enrich_with_deterministic_facts(offer, response.value)
        validate_extraction_completeness(offer, enriched)
        return replace(response, value=enriched)


def validate_model_extraction(offer: CleanJob, extracted: ExtractedJobDetails) -> None:
    """Require the model to map explicit role sections before accepting its JSON."""
    missing = []
    if "ROLE RESPONSIBILITIES:" in offer.analysis_text and not extracted.responsibilities:
        missing.append("responsibilities must map ROLE RESPONSIBILITIES")
    if "ROLE REQUIREMENTS:" in offer.analysis_text and not extracted.required_skills:
        missing.append("required_skills must map ROLE REQUIREMENTS")
    if any(len(signal) > 400 for signal in extracted.risk_signals):
        missing.append("risk_signals must contain concise phrases, not copied sections")
    if missing:
        raise ValueError("; ".join(missing))


def enrich_with_deterministic_facts(
    offer: CleanJob, extracted: ExtractedJobDetails
) -> ExtractedJobDetails:
    """Prefer already-detected explicit facts over asking the model to rediscover them."""
    updates = {}
    compensation = offer.supplemental_info.compensation
    work_conditions = offer.supplemental_info.work_conditions
    if not extracted.salary:
        salary = next((item.evidence for item in compensation if item.category == "salary"), None)
        if salary:
            updates["salary"] = salary
    if not extracted.contract_type:
        contracts = [
            item.category
            for item in compensation
            if item.category.casefold()
            in {"b2b", "uop", "contract of employment", "contract of mandate"}
        ]
        if contracts:
            updates["contract_type"] = ", ".join(dict.fromkeys(contracts))
    if not extracted.work_mode:
        mode = next(
            (
                item.category
                for item in work_conditions
                if item.category.casefold() in {"remote", "hybrid", "on-site", "onsite"}
            ),
            None,
        )
        if mode:
            updates["work_mode"] = mode
    return extracted.model_copy(update=updates)


def validate_extraction_completeness(offer: CleanJob, extracted: ExtractedJobDetails) -> None:
    """Trigger the single repair turn when explicit deterministic facts were omitted."""
    compensation_categories = {
        item.category.casefold() for item in offer.supplemental_info.compensation
    }
    work_categories = {item.category.casefold() for item in offer.supplemental_info.work_conditions}
    missing: list[str] = []
    if "salary" in compensation_categories and not extracted.salary:
        missing.append("salary is explicit in supplemental_info")
    contract_markers = {"b2b", "uop", "contract of employment", "contract of mandate"}
    if compensation_categories & contract_markers and not extracted.contract_type:
        missing.append("contract_type is explicit in supplemental_info")
    if work_categories & {"remote", "hybrid", "on-site", "onsite"} and not extracted.work_mode:
        missing.append("work_mode is explicit in supplemental_info")
    if missing:
        raise ValueError("; ".join(missing))


class LocalModelEvaluator:
    def __init__(self, client: LocalLlmClient, model: str) -> None:
        self.client = client
        self.model = model

    async def evaluate(
        self,
        offer: CleanJob,
        extracted: ExtractedJobDetails,
        profile: CandidateProfile,
    ) -> StructuredLlmResponse[EvaluationDraft]:
        if not profile.approved:
            raise ValueError("candidate profile must be approved before evaluation")
        validate_model_extraction(offer, extracted)
        model_input = {
            "job": build_model_input_snapshot(offer),
            "extraction_gate": "passed",
            "candidate_profile": profile_for_model(profile),
        }
        response = await self.client.structured_completion(
            model=self.model,
            system_prompt=EVALUATOR_SYSTEM_PROMPT,
            user_prompt=(
                EVALUATOR_INSTRUCTION
                + "\n\nEvaluate this job and candidate profile:\n"
                + json.dumps(model_input, ensure_ascii=False, sort_keys=True, default=str)
            ),
            schema=EvaluationDraft,
            schema_name=EVALUATOR_PROMPT_VERSION,
            strict_schema=False,
            max_tokens=750,
            validate=lambda value: validate_evaluation_evidence(offer, profile, value),
        )
        return response


def validate_evaluation_evidence(
    offer: CleanJob, profile: CandidateProfile, draft: EvaluationDraft
) -> None:
    if not draft.evidence:
        raise ValueError("evaluation must contain evidence")
    sources = evaluation_evidence_sources(offer, profile)
    for evidence in draft.evidence:
        quote = " ".join(evidence.quote.split()).casefold()
        if not quote:
            raise ValueError("evidence quote cannot be empty")
        source = " ".join(sources.get(evidence.source_field, "").split()).casefold()
        if evidence.source_field not in sources or quote not in source:
            exact_sources = [
                source_field
                for source_field, source_text in sources.items()
                if quote in " ".join(source_text.split()).casefold()
            ]
            if len(exact_sources) == 1:
                evidence.source_field = exact_sources[0]
                source = " ".join(sources[evidence.source_field].split()).casefold()
            elif evidence.source_field not in sources:
                raise ValueError(f"unknown evidence source: {evidence.source_field}")
        if quote not in source:
            repaired = closest_source_quote(evidence.quote, sources[evidence.source_field])
            if repaired is None:
                raise ValueError(
                    f"evidence quote is not present in {evidence.source_field}: {evidence.quote!r}"
                )
            evidence.quote = repaired


def evaluation_evidence_sources(offer: CleanJob, profile: CandidateProfile) -> dict[str, str]:
    work_conditions = offer.supplemental_info.work_conditions + (
        offer.supplemental_info.travel_requirements
    )
    sources = {
        "offer.analysis_text": offer.analysis_text,
        "offer.description": offer.description,
        "offer.work_conditions": " ".join(item.evidence for item in work_conditions),
    }
    sources.update({f"profile:{item.id}": item.statement for item in profile.evidence})
    sources.update(
        {
            "profile:work_preferences": " ".join(profile.work_preferences),
            "profile:role_direction_preferences": " ".join(
                profile.role_direction_preferences
            ),
            "profile:negative_criteria": " ".join(profile.negative_criteria),
            "profile:transferable_skills": " ".join(profile.transferable_skills),
            "profile:location_rule": profile.location_rule,
        }
    )
    return sources


def closest_source_quote(quote: str, source: str, threshold: float = 0.9) -> str | None:
    """Repair a near-verbatim quote while rejecting loose semantic similarity."""
    quote_words = quote.split()
    source_words = source.split()
    if len(quote_words) < 4 or len(source_words) < len(quote_words):
        return None
    normalized_quote = " ".join(quote_words).casefold()
    best_ratio = 0.0
    best = None
    for width in range(max(4, len(quote_words) - 1), len(quote_words) + 2):
        for start in range(0, len(source_words) - width + 1):
            candidate = " ".join(source_words[start : start + width])
            ratio = SequenceMatcher(None, normalized_quote, candidate.casefold()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = candidate
    return best if best_ratio >= threshold else None


class LocalModelJudge:
    def __init__(self, client: LocalLlmClient, model: str) -> None:
        self.client = client
        self.model = model

    async def review(
        self,
        offer: CleanJob,
        profile: CandidateProfile,
        draft: EvaluationDraft,
    ) -> StructuredLlmResponse[JudgeResult]:
        model_input = {
            "job": build_model_input_snapshot(offer),
            "candidate_profile": profile_for_model(profile),
            "evaluation_draft": draft.model_dump(mode="json"),
        }
        response = await self.client.structured_completion(
            model=self.model,
            system_prompt=JUDGE_SYSTEM_PROMPT,
            user_prompt=(
                JUDGE_INSTRUCTION
                + "\n\nReview this evaluation:\n"
                + json.dumps(model_input, ensure_ascii=False, sort_keys=True, default=str)
            ),
            schema=JudgeResult,
            schema_name=JUDGE_PROMPT_VERSION,
            strict_schema=False,
            max_tokens=900,
        )
        return replace(response, value=normalize_judgment(offer, profile, response.value))


def validate_judgment(offer: CleanJob, profile: CandidateProfile, judgment: JudgeResult) -> None:
    if judgment.approved and judgment.corrected_assessment is not None:
        raise ValueError("approved judgment cannot include a corrected assessment")
    if not judgment.approved and not judgment.issues:
        raise ValueError("rejected judgment must explain at least one issue")
    if judgment.corrected_assessment:
        validate_evaluation_evidence(offer, profile, judgment.corrected_assessment)


def normalize_judgment(
    offer: CleanJob, profile: CandidateProfile, judgment: JudgeResult
) -> JudgeResult:
    """Preserve an imperfect self-review as an explicit warning instead of losing the item."""
    issues = list(judgment.issues)
    corrected = judgment.corrected_assessment
    approved = judgment.approved
    if approved and corrected is not None:
        approved = False
        issues.append("Self-review returned a correction together with approved=true")
    if not approved and not issues:
        issues.append("Self-review rejected the draft without stating a reason")
    if corrected is not None:
        try:
            validate_evaluation_evidence(offer, profile, corrected)
        except ValueError as exc:
            issues.append(f"Discarded ungrounded self-review correction: {exc}")
            corrected = None
            approved = False
    return judgment.model_copy(
        update={"approved": approved, "issues": issues, "corrected_assessment": corrected}
    )


def profile_for_model(profile: CandidateProfile) -> dict:
    """Hide provenance/control metadata while retaining candidate facts and stable ids."""
    payload = profile.model_dump(mode="json", exclude={"approved_at"})
    payload["evidence"] = [
        {"id": evidence.id, "statement": evidence.statement} for evidence in profile.evidence
    ]
    return payload
