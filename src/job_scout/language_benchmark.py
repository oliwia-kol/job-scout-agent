"""Paired multilingual-direct versus canonical-English evaluator benchmark."""

from __future__ import annotations

import json
import statistics
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob
from .domain import CandidateProfile, ExtractedJobDetails, calculate_final_score
from .language import LanguageCode, detect_language
from .local_llm import LocalLlmClient
from .model_tasks import LocalModelEvaluator


class LanguagePair(BaseModel):
    id: str
    source_language: LanguageCode
    source_text: str
    canonical_english: str
    must_preserve: list[str] = Field(min_length=1)


class LanguagePairDataset(BaseModel):
    dataset_id: str
    kind: str
    license: str
    created_for: str
    pairs: list[LanguagePair] = Field(min_length=1)


def load_language_pairs(path: Path) -> LanguagePairDataset:
    return LanguagePairDataset.model_validate_json(path.read_text(encoding="utf-8"))


async def run_language_strategy_benchmark(
    dataset: LanguagePairDataset,
    profile: CandidateProfile,
    *,
    client: LocalLlmClient,
    model: str,
) -> dict:
    evaluator = LocalModelEvaluator(client, model)
    results: list[dict] = []
    for pair in dataset.pairs:
        direct_offer = _offer(pair, pair.source_text, "direct")
        canonical_offer = _offer(pair, pair.canonical_english, "canonical-en")
        facts_preserved = [
            fact
            for fact in pair.must_preserve
            if fact.casefold() in pair.canonical_english.casefold()
        ]
        item = {
            "pair_id": pair.id,
            "source_language": pair.source_language,
            "detected_language": detect_language(pair.source_text).language,
            "facts_expected": len(pair.must_preserve),
            "facts_preserved": len(facts_preserved),
            "direct": None,
            "canonical_en": None,
            "score_delta": None,
            "recommendation_stable": None,
            "error": None,
        }
        try:
            direct = await evaluator.evaluate(direct_offer, ExtractedJobDetails(), profile)
            canonical = await evaluator.evaluate(
                canonical_offer, ExtractedJobDetails(), profile
            )
            item["direct"] = _draft_metrics(direct.value, direct.elapsed_ms, direct.retries)
            item["canonical_en"] = _draft_metrics(
                canonical.value, canonical.elapsed_ms, canonical.retries
            )
            item["score_delta"] = round(
                item["canonical_en"]["final_score"] - item["direct"]["final_score"],
                2,
            )
            item["recommendation_stable"] = (
                canonical.value.recommendation.strip().casefold()
                == direct.value.recommendation.strip().casefold()
            )
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)

    completed = [item for item in results if item["error"] is None]
    deltas = [abs(float(item["score_delta"])) for item in completed]
    stable = sum(bool(item["recommendation_stable"]) for item in completed)
    fact_total = sum(item["facts_expected"] for item in results)
    fact_preserved = sum(item["facts_preserved"] for item in results)
    direct_latency = [
        int(item["direct"]["elapsed_ms"]) for item in completed if item["direct"]
    ]
    canonical_latency = [
        int(item["canonical_en"]["elapsed_ms"])
        for item in completed
        if item["canonical_en"]
    ]
    return {
        "dataset_id": dataset.dataset_id,
        "model": model,
        "total_pairs": len(results),
        "completed_pairs": len(completed),
        "failed_pairs": len(results) - len(completed),
        "translation_fact_preservation_rate": (
            round(fact_preserved / fact_total, 3) if fact_total else 0
        ),
        "recommendation_stability_rate": (
            round(stable / len(completed), 3) if completed else 0
        ),
        "median_absolute_score_delta": (
            round(statistics.median(deltas), 3) if deltas else None
        ),
        "median_direct_latency_ms": (
            round(statistics.median(direct_latency)) if direct_latency else None
        ),
        "median_canonical_latency_ms": (
            round(statistics.median(canonical_latency)) if canonical_latency else None
        ),
        "results": results,
    }


def write_language_benchmark(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _offer(pair: LanguagePair, text: str, path: str) -> CleanJob:
    return CleanJob(
        source_id=f"language-benchmark-{pair.id}",
        company="Synthetic Language Benchmark",
        external_id=f"{pair.id}-{path}",
        title="Applied AI Engineer",
        url=f"https://example.invalid/{pair.id}/{path}",
        locations=["Poland"],
        description=text,
        analysis_text=text,
        raw_sha256=sha256(text.encode("utf-8")).hexdigest(),
        extraction_method="synthetic-controlled",
    )


def _draft_metrics(draft: object, elapsed_ms: int, retries: int) -> dict:
    return {
        "final_score": calculate_final_score(
            draft.opportunity_score,
            draft.cv_fit_score,
            draft.work_conditions_score,
            draft.development_potential_score,
        ),
        "opportunity_score": draft.opportunity_score,
        "cv_fit_score": draft.cv_fit_score,
        "work_conditions_score": draft.work_conditions_score,
        "development_potential_score": draft.development_potential_score,
        "recommendation": draft.recommendation,
        "evidence_count": len(draft.evidence),
        "elapsed_ms": elapsed_ms,
        "retries": retries,
    }
