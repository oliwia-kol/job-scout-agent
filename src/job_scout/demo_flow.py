"""End-to-end evaluator and self-review run over frozen extractor outputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .ats_scrapers import CleanJob
from .domain import (
    CandidateProfile,
    EvaluationItemStatus,
    EvaluationRunItem,
    EvaluationRunStatus,
    ExtractedJobDetails,
)
from .llama_server import LlamaServerManager
from .local_llm import LocalLlmClient
from .model_tasks import LocalModelEvaluator, LocalModelExtractor, LocalModelJudge
from .prefilter import infer_location
from .settings import Settings
from .storage import (
    get_pipeline_run,
    list_evaluation_run_items,
    update_evaluation_run_item,
    update_run_state,
)


def load_extractions(path: Path) -> dict[tuple[str, str], ExtractedJobDetails]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        (item["company"], item["title"]): ExtractedJobDetails.model_validate(item["extracted"])
        for item in payload["items"]
        if item["status"] == "completed" and item.get("extracted")
    }


async def run_demo_flow_db(
    database_path: Path,
    run_id: str,
    offers: list[CleanJob],
    profile: CandidateProfile,
    extractions: dict[tuple[str, str], ExtractedJobDetails],
    settings: Settings,
) -> None:
    db_items = list_evaluation_run_items(database_path, run_id)
    items_by_offer_key = {(item["company"], item["title"]): item for item in db_items}

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

            for offer in offers:
                run_info = get_pipeline_run(database_path, run_id)
                if not run_info or run_info["status"] == EvaluationRunStatus.CANCELLED.value:
                    break

                run_type = run_info.get("run_type")

                key = (offer.company, offer.title)
                db_item_dict = items_by_offer_key.get(key)
                if not db_item_dict:
                    continue

                if db_item_dict["status"] in (
                    EvaluationItemStatus.COMPLETED.value,
                    EvaluationItemStatus.FAILED.value,
                ):
                    continue

                item = EvaluationRunItem(
                    item_id=db_item_dict["item_id"],
                    run_id=db_item_dict["run_id"],
                    offer_id=db_item_dict["offer_id"],
                    status=EvaluationItemStatus.RUNNING,
                    input_sha256=db_item_dict["input_sha256"],
                    input_snapshot=db_item_dict["input_snapshot"],
                    extracted=db_item_dict["extracted"],
                    draft=db_item_dict["draft"],
                    judgment=db_item_dict["judgment"],
                    final_assessment=db_item_dict["final_assessment"],
                    timings_ms=db_item_dict["timings"],
                    retry_counts=db_item_dict["retry_counts"],
                    error=db_item_dict["error"],
                )

                if run_type in {"frozen_full_pipeline", "single_offer_eval", "live_single_offer"}:
                    update_evaluation_run_item(
                        database_path, item, current_stage_label=f"{offer.company}: extractor"
                    )
                    extractor = LocalModelExtractor(client, settings.local_llm_model)
                    try:
                        extraction = await extractor.extract(offer)
                        item.extracted = extraction.value
                        extracted = extraction.value
                        item.timings_ms["extractor"] = extraction.elapsed_ms
                        item.retry_counts["extractor"] = extraction.retries
                    except Exception as exc:
                        item.status = EvaluationItemStatus.FAILED
                        item.error = f"Extractor failed: {type(exc).__name__}: {exc}"
                        update_evaluation_run_item(
                            database_path,
                            item,
                            is_completed=True,
                            current_stage_label=f"{offer.company}: failed",
                        )
                        continue
                else:
                    extracted = extractions.get(key)
                    if not extracted:
                        item.status = EvaluationItemStatus.FAILED
                        item.error = "missing frozen extraction"
                        update_evaluation_run_item(
                            database_path,
                            item,
                            is_completed=True,
                            current_stage_label=f"{offer.company}: failed",
                        )
                        continue
                    item.extracted = extracted

                update_evaluation_run_item(
                    database_path, item, current_stage_label=f"{offer.company}: evaluator"
                )

                try:
                    evaluation = await evaluator.evaluate(offer, extracted, profile)
                    item.draft = evaluation.value
                    item.timings_ms["evaluator"] = evaluation.elapsed_ms
                    item.retry_counts["evaluator"] = evaluation.retries
                    update_evaluation_run_item(
                        database_path, item, current_stage_label=f"{offer.company}: self-review"
                    )

                    judgment = await judge.review(offer, profile, evaluation.value)
                    item.judgment = judgment.value
                    item.timings_ms["judge"] = judgment.elapsed_ms
                    item.retry_counts["judge"] = judgment.retries

                    final_draft = judgment.value.corrected_assessment or evaluation.value
                    item.final_assessment = final_draft.to_assessment(infer_location(offer))
                    item.status = EvaluationItemStatus.COMPLETED
                except Exception as exc:
                    item.status = EvaluationItemStatus.FAILED
                    item.error = f"{type(exc).__name__}: {exc}"

                update_evaluation_run_item(
                    database_path,
                    item,
                    is_completed=True,
                    current_stage_label=f"{offer.company}: {item.status.value}",
                )

    run_info = get_pipeline_run(database_path, run_id)
    if run_info and run_info["status"] != EvaluationRunStatus.CANCELLED.value:
        final_items = list_evaluation_run_items(database_path, run_id)
        any_failed = any(it["status"] == EvaluationItemStatus.FAILED.value for it in final_items)
        final_status = EvaluationRunStatus.FAILED if any_failed else EvaluationRunStatus.COMPLETED
        update_run_state(
            database_path,
            run_id,
            status=final_status.value,
            current_stage="finished",
            finished_at=datetime.now(UTC),
        )
