"""Fast single-call evaluation for user-visible offers."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

from .ats_scrapers import CleanJob
from .domain import (
    CandidateProfile,
    EvaluationItemStatus,
    EvaluationRunItem,
    EvaluationRunStatus,
    ScoreEvidence,
)
from .gemini_role_fit import MODEL as GEMINI_MODEL
from .gemini_role_fit import evaluate_gemini
from .llama_server import LlamaServerManager
from .local_llm import LocalLlmClient
from .model_tasks import validate_evaluation_evidence
from .prefilter import infer_location
from .requirement_matrix import unconfirmed_hard_requirements
from .role_direction import SOFTWARE_TITLE, classify_role, has_clear_applied_duties
from .role_fit import (
    SYSTEM_PROMPT,
    RoleFitAnswer,
    answer_to_draft,
    constrained_schema,
    model_input,
    validate_answer,
)
from .settings import Settings
from .storage import (
    get_offer,
    get_pipeline_run,
    list_evaluation_run_items,
    update_evaluation_run_item,
    update_run_state,
)


def guard_role_answer(
    answer: RoleFitAnswer, offer: CleanJob, catalog: dict[str, str]
) -> RoleFitAnswer:
    role_gate = classify_role(offer.title, offer.analysis_text)
    if role_gate.category == "software" and answer.direction != "software":
        return answer.model_copy(update={"direction": "software", "fit": "weak"})
    if (
        role_gate.category == "target"
        and has_clear_applied_duties(offer.analysis_text)
        and answer.direction in {"software", "other"}
    ):
        supporting_id = next(
            (key for key, quote in catalog.items() if has_clear_applied_duties(quote)),
            answer.offer_evidence_id,
        )
        return answer.model_copy(
            update={
                "direction": "target",
                "fit": "possible",
                "offer_evidence_id": supporting_id,
            }
        )
    if (
        role_gate.category == "target"
        and answer.direction == "software"
        and (
            not SOFTWARE_TITLE.search(offer.title)
            or has_clear_applied_duties(offer.analysis_text)
        )
    ):
        return answer.model_copy(update={"direction": "ambiguous", "fit": "unknown"})
    return answer


def add_source_grounded_cv_gaps(draft, offer: CleanJob, profile: CandidateProfile):
    """Downgrade a fit that ignores explicit seniority or named-tool requirements."""
    detected = unconfirmed_hard_requirements(offer, profile)
    if not detected:
        return draft
    gaps = list(draft.gaps)
    evidence = list(draft.evidence)
    for requirement in detected:
        quote = requirement.text
        if quote.casefold() not in offer.description.casefold():
            continue
        if not any(quote.casefold() in gap.casefold() for gap in gaps):
            gaps.append(quote)
            evidence.append(
                ScoreEvidence(
                    criterion="mandatory requirement not confirmed in CV",
                    source_field="offer.description",
                    quote=quote,
                )
            )
    if len(gaps) == len(draft.gaps):
        return draft
    return draft.model_copy(
        update={
            "gaps": gaps,
            "evidence": evidence,
            "cv_fit_score": min(draft.cv_fit_score, 5),
            "confidence": min(draft.confidence, 0.5),
            "recommendation": (
                "consider" if draft.recommendation == "apply" else draft.recommendation
            ),
        }
    )


def cap_unresolved_direction(draft, offer: CleanJob):
    """A role needing manual direction review cannot receive a confident AI score."""
    if classify_role(offer.title, offer.analysis_text).category != "review":
        return draft
    return draft.model_copy(
        update={
            "opportunity_score": min(draft.opportunity_score, 5),
            "applied_ai_score": min(draft.applied_ai_score, 5),
            "ai_automation_score": min(draft.ai_automation_score, 5),
            "confidence": min(draft.confidence, 0.5),
            "recommendation": (
                "consider" if draft.recommendation == "apply" else draft.recommendation
            ),
        }
    )


async def run_product_evaluation(
    database_path: Path, run_id: str, profile: CandidateProfile, settings: Settings
) -> None:
    items = list_evaluation_run_items(database_path, run_id)
    async with AsyncExitStack() as stack:
        client = None
        if settings.evaluation_model != GEMINI_MODEL:
            await stack.enter_async_context(
                LlamaServerManager(
                    executable=settings.llama_server_executable,
                    model_path=settings.evaluation_model_path,
                    base_url=settings.local_llm_base_url,
                    context_size=max(settings.local_llm_context_size, 8192),
                    reasoning=False,
                    reasoning_budget=0,
                )
            )
            client = await stack.enter_async_context(
                LocalLlmClient(settings.local_llm_base_url, timeout_seconds=120)
            )
        for source in items:
            state = get_pipeline_run(database_path, run_id)
            if not state or state["status"] == EvaluationRunStatus.CANCELLED.value:
                break
            if source["status"] in {"completed", "failed"}:
                continue
            item = EvaluationRunItem(
                item_id=source["item_id"],
                run_id=run_id,
                offer_id=source["offer_id"],
                input_sha256=source["input_sha256"],
                input_snapshot=source["input_snapshot"],
                status=EvaluationItemStatus.RUNNING,
            )
            stored = get_offer(database_path, item.offer_id)
            if not stored or stored["current_content_sha256"] != item.input_snapshot.get(
                "content_sha256"
            ):
                item.status = EvaluationItemStatus.FAILED
                item.error = "Offer changed before evaluation"
                update_evaluation_run_item(database_path, item, is_completed=True)
                continue
            try:
                offer = CleanJob.model_validate(stored["offer"])
                if settings.evaluation_model == GEMINI_MODEL:
                    answer, catalog, elapsed_ms = await asyncio.to_thread(
                        evaluate_gemini, offer, profile
                    )
                    retries = 0
                else:
                    assert client is not None
                    prompt, catalog = model_input(offer, profile)
                    response = await client.structured_completion(
                        model=settings.evaluation_model,
                        system_prompt=SYSTEM_PROMPT,
                        user_prompt=prompt,
                        schema=RoleFitAnswer,
                        schema_name="role-fit-v1",
                        max_tokens=500,
                        json_schema_override=constrained_schema(catalog, profile),
                        validate=lambda value: validate_answer(value, catalog, profile),
                    )
                    answer, elapsed_ms, retries = (
                        response.value,
                        response.elapsed_ms,
                        response.retries,
                    )
                answer = guard_role_answer(answer, offer, catalog)
                draft = answer_to_draft(answer, catalog, profile)
                if answer.direction == "target":
                    draft = add_source_grounded_cv_gaps(draft, offer, profile)
                draft = cap_unresolved_direction(draft, offer)
                # No tested evaluator has passed the visibility-quality gate yet.
                # Keep the score informative without issuing a confident apply recommendation.
                draft = draft.model_copy(
                    update={
                        "confidence": min(draft.confidence, 0.5),
                        "recommendation": (
                            "consider" if draft.recommendation == "apply" else draft.recommendation
                        ),
                    }
                )
                validate_evaluation_evidence(offer, profile, draft)
                item.draft = draft
                item.final_assessment = draft.to_assessment(infer_location(offer))
                item.timings_ms["evaluator"] = elapsed_ms
                item.retry_counts["evaluator"] = retries
                item.status = EvaluationItemStatus.COMPLETED
            except Exception as exc:
                item.status = EvaluationItemStatus.FAILED
                item.error = f"{type(exc).__name__}: {exc}"
            update_evaluation_run_item(
                database_path,
                item,
                is_completed=True,
                current_stage_label=f"{stored['company']}: {item.status.value}",
            )
    state = get_pipeline_run(database_path, run_id)
    if state and state["status"] != EvaluationRunStatus.CANCELLED.value:
        final_items = list_evaluation_run_items(database_path, run_id)
        failed = any(item["status"] == "failed" for item in final_items)
        update_run_state(
            database_path,
            run_id,
            status=(EvaluationRunStatus.FAILED if failed else EvaluationRunStatus.COMPLETED).value,
            current_stage="finished",
            finished_at=datetime.now(UTC),
        )
