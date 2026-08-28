"""Domain contracts for the local-first Job Scout pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class LocationEligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class ApplicationStatus(StrEnum):
    NEW = "new"
    SAVED = "saved"
    REJECTED = "rejected"
    APPLYING = "applying"
    APPLIED = "applied"
    REVIEW_LATER = "review_later"


class EvaluationRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class EvaluationItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class CandidateEvidence(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    statement: str = Field(min_length=1)
    source: str = Field(min_length=1)


class CandidateProfile(BaseModel):
    profile_id: str
    version: int = Field(ge=1)
    target_roles: list[str] = Field(min_length=1)
    evidence: list[CandidateEvidence] = Field(min_length=1)
    transferable_skills: list[str] = Field(default_factory=list)
    work_preferences: list[str] = Field(default_factory=list)
    role_direction_preferences: list[str] = Field(default_factory=list)
    negative_criteria: list[str] = Field(default_factory=list)
    location_rule: str = Field(min_length=1)
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def evidence_ids_are_unique(self) -> CandidateProfile:
        ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate evidence ids must be unique")
        return self

    @property
    def approved(self) -> bool:
        return self.approved_at is not None


class RawOffer(BaseModel):
    source_id: str
    company: str
    source_url: HttpUrl
    job_url: HttpUrl
    external_id: str | None = None
    title_hint: str | None = None
    payload: str
    content_type: str = "text/html"
    fetched_at: datetime = Field(default_factory=utc_now)
    payload_sha256: str


class JobOffer(BaseModel):
    company: str
    title: str
    job_url: HttpUrl
    description: str
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_from_poland: bool | None = None
    office_days_per_month: int | None = Field(default=None, ge=0)
    salary: str | None = None
    contract_type: str | None = None
    seniority: str | None = None
    published_at: datetime | None = None


class ScoreEvidence(BaseModel):
    criterion: str
    quote: str
    source_field: str


class ExtractedJobDetails(BaseModel):
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    seniority: str | None = None
    contract_type: str | None = None
    salary: str | None = None
    work_mode: str | None = None
    office_days_per_month: int | None = Field(default=None, ge=0)
    risk_signals: list[str] = Field(default_factory=list)


class FitAssessment(BaseModel):
    opportunity_score: float = Field(ge=0, le=10)
    cv_fit_score: float = Field(ge=0, le=10)
    work_conditions_score: float = Field(ge=0, le=10)
    development_potential_score: float | None = Field(default=None, ge=0, le=10)
    final_score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    location_eligibility: LocationEligibility
    applied_ai_score: float = Field(ge=0, le=10)
    ai_automation_score: float = Field(ge=0, le=10)
    ai_evaluation_score: float = Field(ge=0, le=10)
    data_ml_score: float = Field(ge=0, le=10)
    ai_research_score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    evidence: list[ScoreEvidence] = Field(default_factory=list)
    recommendation: str

    @model_validator(mode="after")
    def ineligible_offer_cannot_alert(self) -> FitAssessment:
        if self.location_eligibility == LocationEligibility.INELIGIBLE and self.final_score >= 8:
            raise ValueError("location-ineligible offer cannot have an alerting score")
        return self

    @property
    def can_alert(self) -> bool:
        return (
            self.final_score >= 8
            and self.location_eligibility == LocationEligibility.ELIGIBLE
            and self.confidence >= 0.8
        )


def calculate_final_score(
    opportunity_score: float,
    cv_fit_score: float,
    work_conditions_score: float,
    development_potential_score: float | None = None,
) -> float:
    """Apply the demo's fixed ranking weights and keep a stable one-decimal score."""
    for value in (opportunity_score, cv_fit_score, work_conditions_score):
        if not 0 <= value <= 10:
            raise ValueError("score components must be between 0 and 10")
    if development_potential_score is not None:
        if not 0 <= development_potential_score <= 10:
            raise ValueError("score components must be between 0 and 10")
        return round(
            opportunity_score * 0.35
            + cv_fit_score * 0.25
            + work_conditions_score * 0.2
            + development_potential_score * 0.2,
            1,
        )
    return round(
        opportunity_score * 0.5 + cv_fit_score * 0.3 + work_conditions_score * 0.2,
        1,
    )


class EvaluationDraft(BaseModel):
    opportunity_score: float = Field(ge=0, le=10)
    cv_fit_score: float = Field(ge=0, le=10)
    work_conditions_score: float = Field(ge=0, le=10)
    development_potential_score: float | None = Field(default=None, ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    applied_ai_score: float = Field(ge=0, le=10)
    ai_automation_score: float = Field(ge=0, le=10)
    ai_evaluation_score: float = Field(ge=0, le=10)
    data_ml_score: float = Field(ge=0, le=10)
    ai_research_score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    evidence: list[ScoreEvidence] = Field(default_factory=list)
    recommendation: str

    def to_assessment(self, location: LocationEligibility) -> FitAssessment:
        values = self.model_dump()
        values["final_score"] = calculate_final_score(
            self.opportunity_score,
            self.cv_fit_score,
            self.work_conditions_score,
            self.development_potential_score,
        )
        values["location_eligibility"] = location
        return FitAssessment(**values)


class JudgeResult(BaseModel):
    approved: bool
    confidence: float = Field(ge=0, le=1)
    corrected_assessment: EvaluationDraft | None = None
    issues: list[str] = Field(default_factory=list)


class ModelRunConfig(BaseModel):
    model_name: str
    model_path: str
    model_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    context_size: int = Field(default=16384, ge=1)
    seed: int = 42
    temperature: float = Field(default=0, ge=0)
    max_parallel_requests: int = Field(default=1, ge=1)
    prompt_versions: dict[str, str] = Field(default_factory=dict)


class EvaluationRunItem(BaseModel):
    item_id: str
    run_id: str
    offer_id: int
    status: EvaluationItemStatus = EvaluationItemStatus.PENDING
    input_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    input_snapshot: dict[str, Any]
    extracted: ExtractedJobDetails | None = None
    draft: EvaluationDraft | None = None
    judgment: JudgeResult | None = None
    final_assessment: FitAssessment | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class PipelineRun(BaseModel):
    run_id: str
    source_id: str
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    run_type: str = "demo"
    status: EvaluationRunStatus = EvaluationRunStatus.RUNNING
    total_items: int = Field(default=0, ge=0)
    completed_items: int = Field(default=0, ge=0)
    current_stage: str | None = None
    profile_id: str | None = None
    profile_version: int | None = Field(default=None, ge=1)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    mlflow_run_id: str | None = None
    model_configurations: dict[str, Any] = Field(default_factory=dict)
    parent_run_id: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def progress_cannot_exceed_total(self) -> PipelineRun:
        if self.completed_items > self.total_items:
            raise ValueError("completed items cannot exceed total items")
        return self
