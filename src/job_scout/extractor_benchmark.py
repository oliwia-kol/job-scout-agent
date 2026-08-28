"""Sequential quality benchmark for the frozen demo extractor sample."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob
from .domain import ExtractedJobDetails
from .llama_server import LlamaServerManager
from .local_llm import LocalLlmClient
from .model_tasks import LocalModelExtractor
from .prompts import EXTRACTOR_PROMPT_VERSION
from .settings import Settings


class ExtractorBenchmarkItem(BaseModel):
    company: str
    title: str
    raw_sha256: str
    status: str
    extracted: ExtractedJobDetails | None = None
    elapsed_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retries: int = 0
    quality_flags: list[str] = Field(default_factory=list)
    error: str | None = None


class ExtractorBenchmarkReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    model: str
    model_sha256: str
    prompt_version: str
    total: int
    completed: int
    failed: int
    items: list[ExtractorBenchmarkItem]


def extraction_quality_flags(value: ExtractedJobDetails) -> list[str]:
    flags: list[str] = []
    if not value.responsibilities:
        flags.append("empty_responsibilities")
    if not value.required_skills:
        flags.append("empty_required_skills")
    if any(len(signal) > 400 for signal in value.risk_signals):
        flags.append("oversized_risk_signal")
    if value.salary and len(value.salary) > 300:
        flags.append("oversized_salary_evidence")
    return flags


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def run_extractor_benchmark(
    offers: list[CleanJob], settings: Settings, progress_path: Path | None = None
) -> ExtractorBenchmarkReport:
    started_at = datetime.now(UTC)
    items: list[ExtractorBenchmarkItem] = []
    model_hash = file_sha256(settings.local_llm_model_path)
    async with LlamaServerManager(
        executable=settings.llama_server_executable,
        model_path=settings.local_llm_model_path,
        base_url=settings.local_llm_base_url,
    ):
        async with LocalLlmClient(settings.local_llm_base_url) as client:
            extractor = LocalModelExtractor(client, settings.local_llm_model)
            for offer in offers:
                try:
                    response = await extractor.extract(offer)
                    items.append(
                        ExtractorBenchmarkItem(
                            company=offer.company,
                            title=offer.title,
                            raw_sha256=offer.raw_sha256,
                            status="completed",
                            extracted=response.value,
                            elapsed_ms=response.elapsed_ms,
                            prompt_tokens=response.prompt_tokens,
                            completion_tokens=response.completion_tokens,
                            retries=response.retries,
                            quality_flags=extraction_quality_flags(response.value),
                        )
                    )
                except Exception as exc:
                    items.append(
                        ExtractorBenchmarkItem(
                            company=offer.company,
                            title=offer.title,
                            raw_sha256=offer.raw_sha256,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                if progress_path:
                    write_benchmark_report(
                        _build_report(started_at, offers, items, settings, model_hash),
                        progress_path,
                    )
    return _build_report(started_at, offers, items, settings, model_hash)


def _build_report(
    started_at: datetime,
    offers: list[CleanJob],
    items: list[ExtractorBenchmarkItem],
    settings: Settings,
    model_hash: str,
) -> ExtractorBenchmarkReport:
    completed = sum(item.status == "completed" for item in items)
    failed = sum(item.status == "failed" for item in items)
    return ExtractorBenchmarkReport(
        started_at=started_at,
        finished_at=datetime.now(UTC),
        model=settings.local_llm_model,
        model_sha256=model_hash,
        prompt_version=EXTRACTOR_PROMPT_VERSION,
        total=len(offers),
        completed=completed,
        failed=failed,
        items=list(items),
    )


def write_benchmark_report(report: ExtractorBenchmarkReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
