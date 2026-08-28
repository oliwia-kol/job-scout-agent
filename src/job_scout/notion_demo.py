"""Notion-first, local-model demo runner without a web application layer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .ats_scrapers import CleanJob
from .domain import CandidateProfile
from .llama_server import LlamaServerManager
from .local_llm import LocalLlmClient
from .model_tasks import LocalModelEvaluator, LocalModelExtractor, LocalModelJudge
from .notion_store import NotionOffersStore, NotionPage
from .prefilter import infer_location
from .settings import Settings
from .telegram_integration import send_fit_alert


@dataclass(frozen=True)
class NotionDemoResult:
    offer: CleanJob
    status: str
    score: float | None = None
    error: str | None = None


async def _run_stage(awaitable, timeout_seconds: float):
    """Allow unrestricted generation while preventing one stuck offer from blocking a run."""
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


async def run_notion_demo(
    offers: list[CleanJob],
    profile: CandidateProfile,
    settings: Settings,
    store: NotionOffersStore,
) -> list[NotionDemoResult]:
    """Evaluate only new or changed offers and use Notion as the user-facing state store."""
    if not profile.approved:
        raise ValueError("candidate profile must be approved before evaluation")

    results: list[NotionDemoResult] = []
    pending: list[tuple[CleanJob, NotionPage, str]] = []
    for offer in offers:
        content_hash = store.content_hash(offer)
        existing = store.lookup_offer(offer)
        if store.is_current(existing, content_hash):
            results.append(NotionDemoResult(offer=offer, status="skipped_unchanged"))
            continue
        page = store.upsert_offer(offer, content_hash=content_hash, existing_page=existing)
        pending.append((offer, page, content_hash))

    if not pending:
        return results

    extracted = {}
    async with LlamaServerManager(
        executable=settings.llama_server_executable,
        model_path=settings.scout_llm_model_path,
        base_url=settings.local_llm_base_url,
        context_size=settings.local_llm_context_size,
        reasoning=False,
    ):
        async with LocalLlmClient(settings.local_llm_base_url) as client:
            extractor = LocalModelExtractor(client, settings.scout_llm_model)
            for offer, page, _ in pending:
                try:
                    extracted[(offer.company, offer.title)] = await _run_stage(
                        extractor.extract(offer), settings.local_llm_stage_timeout_seconds
                    )
                except Exception as exc:
                    error = f"Extractor failed: {type(exc).__name__}: {exc}"
                    store.mark_failed(page.page_id, error)
                    results.append(
                        NotionDemoResult(
                            offer=offer,
                            status="failed",
                            error=error,
                        )
                    )

    async with LlamaServerManager(
        executable=settings.llama_server_executable,
        model_path=settings.local_llm_model_path,
        base_url=settings.local_llm_base_url,
        context_size=settings.local_llm_context_size,
        reasoning=settings.local_llm_reasoning,
        reasoning_budget=settings.local_llm_reasoning_budget,
    ):
        async with LocalLlmClient(settings.local_llm_base_url) as client:
            evaluator = LocalModelEvaluator(client, settings.local_llm_model)
            judge = LocalModelJudge(client, settings.local_llm_model)
            for offer, page, content_hash in pending:
                extraction = extracted.get((offer.company, offer.title))
                if extraction is None:
                    continue
                try:
                    draft = (
                        await _run_stage(
                            evaluator.evaluate(offer, extraction.value, profile),
                            settings.local_llm_stage_timeout_seconds,
                        )
                    ).value
                    judgment = (
                        await _run_stage(
                            judge.review(offer, profile, draft),
                            settings.local_llm_stage_timeout_seconds,
                        )
                    ).value
                    assessment = (judgment.corrected_assessment or draft).to_assessment(
                        infer_location(offer)
                    )
                    store.save_assessment(page.page_id, assessment)

                    if assessment.can_alert:
                        alert_key = store.alert_key(offer, content_hash)
                        sent = (
                            page.alert_key == alert_key and page.alert_status == "Sent"
                        ) or send_fit_alert(
                            offer,
                            assessment,
                            token=settings.telegram_bot_token,
                            chat_id=settings.telegram_chat_id,
                        )
                        if not (page.alert_key == alert_key and page.alert_status == "Sent"):
                            store.mark_alert(page.page_id, alert_key=alert_key, sent=sent)
                    results.append(
                        NotionDemoResult(
                            offer=offer, status="completed", score=assessment.final_score
                        )
                    )
                except Exception as exc:
                    error = f"Evaluation failed: {type(exc).__name__}: {exc}"
                    store.mark_failed(page.page_id, error)
                    results.append(
                        NotionDemoResult(
                            offer=offer,
                            status="failed",
                            error=error,
                        )
                    )
    return results
