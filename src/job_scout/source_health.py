"""Source smoke tests and machine-readable health reports."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob, HttpClient, adapter_for, clean_job
from .sources import SourceConfig


class SourceHealth(BaseModel):
    source_id: str
    company: str
    status: str
    checked_at: datetime
    latency_ms: int
    jobs_found: int = 0
    sample: CleanJob | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


async def check_source(source: SourceConfig, http: HttpClient) -> SourceHealth:
    started = time.monotonic()
    try:
        jobs = await adapter_for(source, http).discover()
        sample = await clean_job(jobs[0], http) if jobs else None
        warnings = [] if jobs else ["source reachable but no job links were discovered"]
        status = "ok" if jobs and sample else "degraded"
        return SourceHealth(
            source_id=source.id,
            company=source.company,
            status=status,
            checked_at=datetime.now(UTC),
            latency_ms=round((time.monotonic() - started) * 1000),
            jobs_found=len(jobs),
            sample=sample,
            warnings=warnings,
        )
    except Exception as exc:
        return SourceHealth(
            source_id=source.id,
            company=source.company,
            status="error",
            checked_at=datetime.now(UTC),
            latency_ms=round((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )


async def scan_sources(sources: list[SourceConfig]) -> list[SourceHealth]:
    async with HttpClient() as http:
        semaphore = asyncio.Semaphore(4)

        async def guarded(source: SourceConfig) -> SourceHealth:
            async with semaphore:
                return await check_source(source, http)

        return await asyncio.gather(*(guarded(source) for source in sources if source.enabled))


def write_report(results: list[SourceHealth], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [result.model_dump(mode="json") for result in results]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
