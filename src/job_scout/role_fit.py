"""One-call, evidence-bound fit check for the approved CV profile."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel

from .ats_scrapers import CleanJob
from .domain import CandidateProfile, EvaluationDraft, ScoreEvidence

PROMPT_VERSION = "role-fit-evidence-v1"
SYSTEM_PROMPT = (
    "Assess dominant daily duties and candidate fit. target=practical AI agents, "
    "integrations, GenAI systems or AI automation; software=predominantly backend, "
    "frontend, full-stack, infrastructure or traditional QA; other=management, "
    "presales, classical ML or another direction; ambiguous=insufficient or mixed duties. "
    "Coding alone does not make a role software. Never invent candidate skills. "
    "Select exact supplied evidence IDs; use empty string when no support exists. "
    "A screening gap must be an actual mandatory requirement missing from the profile. "
    "Return only JSON."
)


class RoleFitAnswer(BaseModel):
    direction: Literal["target", "software", "other", "ambiguous"]
    fit: Literal["strong", "possible", "weak", "unknown"]
    offer_evidence_id: str
    profile_evidence_id: str
    gap_evidence_id: str


def evidence_catalog(text: str, *, limit: int = 44) -> dict[str, str]:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+|(?<=:)\s+|\s+[•·]\s+", text)
    snippets: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) < 18:
            continue
        for offset in range(0, len(part), 280):
            piece = part[offset : offset + 280].strip()
            if len(piece) >= 18:
                snippets.append(piece)
    return {f"job-{index}": value for index, value in enumerate(snippets[:limit], 1)}


def model_input(offer: CleanJob, profile: CandidateProfile) -> tuple[str, dict[str, str]]:
    catalog = evidence_catalog(offer.analysis_text)
    payload = {
        "title": offer.title,
        "company": offer.company,
        "locations": offer.locations,
        "offer_evidence": catalog,
        "candidate": {
            "target_roles": profile.target_roles,
            "role_direction_preferences": profile.role_direction_preferences,
            "negative_criteria": profile.negative_criteria,
            "evidence": {item.id: item.statement for item in profile.evidence},
        },
    }
    return json.dumps(payload, ensure_ascii=False), catalog


def validate_answer(
    answer: RoleFitAnswer, catalog: dict[str, str], profile: CandidateProfile
) -> None:
    if answer.offer_evidence_id not in catalog:
        raise ValueError("offer evidence ID does not exist")
    if answer.profile_evidence_id and answer.profile_evidence_id not in {
        item.id for item in profile.evidence
    }:
        raise ValueError("profile evidence ID does not exist")
    if answer.gap_evidence_id and answer.gap_evidence_id not in catalog:
        raise ValueError("gap evidence ID does not exist")


def constrained_schema(catalog: dict[str, str], profile: CandidateProfile) -> dict:
    """Constrain evidence handles at decoding time, including the abstain option."""
    schema = RoleFitAnswer.model_json_schema()
    job_ids = list(catalog)
    profile_ids = [item.id for item in profile.evidence]
    schema["properties"]["offer_evidence_id"]["enum"] = job_ids
    schema["properties"]["profile_evidence_id"]["enum"] = ["", *profile_ids]
    schema["properties"]["gap_evidence_id"]["enum"] = ["", *job_ids]
    return schema


def answer_to_draft(
    answer: RoleFitAnswer, catalog: dict[str, str], profile: CandidateProfile
) -> EvaluationDraft:
    validate_answer(answer, catalog, profile)
    profile_fact = next(
        (item for item in profile.evidence if item.id == answer.profile_evidence_id), None
    )
    direction_score = {"target": 8, "ambiguous": 5, "other": 3, "software": 2}[answer.direction]
    fit_score = {"strong": 8, "possible": 6, "weak": 3, "unknown": 5}[answer.fit]
    if answer.direction != "target":
        fit_score = min(fit_score, direction_score)
    if not profile_fact:
        fit_score = min(fit_score, 5)
    if answer.gap_evidence_id:
        fit_score = min(fit_score, 5)
    evidence = [
        ScoreEvidence(
            criterion="dominant duties",
            source_field="offer.analysis_text",
            quote=catalog[answer.offer_evidence_id],
        )
    ]
    if profile_fact:
        evidence.append(
            ScoreEvidence(
                criterion="confirmed experience",
                source_field=f"profile:{profile_fact.id}",
                quote=profile_fact.statement,
            )
        )
    if answer.gap_evidence_id:
        evidence.append(
            ScoreEvidence(
                criterion="screening gap",
                source_field="offer.analysis_text",
                quote=catalog[answer.gap_evidence_id],
            )
        )
    return EvaluationDraft(
        opportunity_score=direction_score,
        cv_fit_score=fit_score,
        work_conditions_score=5,
        development_potential_score=5,
        confidence=0.8 if answer.direction != "ambiguous" else 0.5,
        applied_ai_score=direction_score,
        ai_automation_score=direction_score,
        ai_evaluation_score=5,
        data_ml_score=5,
        ai_research_score=3,
        strengths=[profile_fact.statement] if profile_fact else [],
        gaps=[catalog[answer.gap_evidence_id]] if answer.gap_evidence_id else [],
        evidence=evidence,
        recommendation=(
            "consider"
            if answer.direction == "ambiguous"
            else "apply"
            if direction_score >= 8 and fit_score >= 6
            else "low_priority"
        ),
    )
