"""Autonomous discovery and clean-JSON collection."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob, DiscoveredJob, HttpClient, adapter_for, clean_job
from .prefilter import PrefilterResult, evaluate_prefilter
from .role_direction import classify_role
from .sources import SourceConfig
from .title_filter import match_title


class CollectionError(BaseModel):
    source_id: str
    company: str
    url: str | None = None
    error: str


class RejectedOffer(BaseModel):
    source_id: str
    company: str
    title: str
    url: str
    stage: str
    reasons: list[str] = Field(default_factory=list)
    prefilter: PrefilterResult | None = None


class SourceObservation(BaseModel):
    """One source's discovery result, including identities used for availability tracking."""

    source_id: str
    company: str
    status: str
    discovered_count: int = 0
    selected_count: int = 0
    observed_jobs: list[DiscoveredJob] = Field(default_factory=list, exclude=True)
    error: str | None = None


class CollectionResult(BaseModel):
    offers: list[CleanJob]
    errors: list[CollectionError]
    rejected: list[RejectedOffer] = Field(default_factory=list)
    observations: list[SourceObservation] = Field(default_factory=list)


def discovery_priority(job) -> tuple[int, int, str]:
    text = f"{job.title} {job.location_hint or ''}".casefold()
    ai_markers = ("ai", "ml", "machine learning", "data scientist", "agent", "llm", "genai")
    location_markers = ("poland", "warsaw", "warszawa", "remote")
    return (
        -sum(marker in text for marker in ai_markers),
        -sum(marker in text for marker in location_markers),
        job.title.casefold(),
    )


async def collect_sources(
    sources: list[SourceConfig],
    limit_per_source: int | None = None,
    *,
    apply_prefilter: bool = True,
    max_candidates_per_source: int | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> CollectionResult:
    offers: list[CleanJob] = []
    errors: list[CollectionError] = []
    rejected: list[RejectedOffer] = []
    observations: list[SourceObservation] = []
    enabled_sources = [source for source in sources if source.enabled]
    async with HttpClient() as http:
        for source_index, source in enumerate(enabled_sources, start=1):
            if progress_callback:
                progress_callback(source.company, source_index - 1, len(enabled_sources))
            try:
                discovered = await adapter_for(source, http).discover()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                errors.append(
                    CollectionError(source_id=source.id, company=source.company, error=message)
                )
                observations.append(
                    SourceObservation(
                        source_id=source.id, company=source.company, status="error", error=message
                    )
                )
                if progress_callback:
                    progress_callback(source.company, source_index, len(enabled_sources))
                continue
            if not discovered:
                if source.options.get("empty_is_valid") is True:
                    observations.append(
                        SourceObservation(
                            source_id=source.id,
                            company=source.company,
                            status="ok",
                            error="source explicitly reports no open roles",
                        )
                    )
                    if progress_callback:
                        progress_callback(source.company, source_index, len(enabled_sources))
                    continue
                errors.append(
                    CollectionError(
                        source_id=source.id,
                        company=source.company,
                        error=(
                            "no job offers discovered; source may be blocked or temporarily empty"
                        ),
                    )
                )
                observations.append(
                    SourceObservation(source_id=source.id, company=source.company, status="empty")
                )
                if progress_callback:
                    progress_callback(source.company, source_index, len(enabled_sources))
                continue
            observation = SourceObservation(
                source_id=source.id,
                company=source.company,
                status="ok",
                discovered_count=len(discovered),
                observed_jobs=discovered,
            )
            observations.append(observation)
            if apply_prefilter and source.adapter != "justjoin":
                title_matches = []
                for job in discovered:
                    signals = match_title(job.title)
                    if signals:
                        title_matches.append(job)
                    else:
                        rejected.append(
                            RejectedOffer(
                                source_id=source.id,
                                company=source.company,
                                title=job.title,
                                url=str(job.url),
                                stage="title",
                                reasons=["title does not match AI/ML role keywords"],
                            )
                        )
                discovered = title_matches
            if not discovered:
                if progress_callback:
                    progress_callback(source.company, source_index, len(enabled_sources))
                continue
            discovered.sort(key=discovery_priority)
            if max_candidates_per_source is not None:
                discovered = discovered[:max_candidates_per_source]
            accepted_for_source = 0
            for job in discovered:
                try:
                    offer = await clean_job(job, http)
                except Exception as exc:
                    errors.append(
                        CollectionError(
                            source_id=source.id,
                            company=source.company,
                            url=str(job.url),
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                prefilter = evaluate_prefilter(offer)
                if source.adapter == "justjoin":
                    role = classify_role(offer.title, offer.analysis_text)
                    offer.role_direction = role.category
                    offer.role_reason = role.reason
                    if apply_prefilter and role.category == "software":
                        rejected.append(
                            RejectedOffer(
                                source_id=source.id,
                                company=offer.company,
                                title=offer.title,
                                url=str(offer.url),
                                stage="role",
                                reasons=[role.reason],
                            )
                        )
                        # Keep the full ad reviewable even though the default offer view hides it.
                        offers.append(offer)
                        continue
                if (
                    apply_prefilter
                    and not prefilter.passed
                    and (
                        source.adapter != "justjoin"
                        or prefilter.location_eligibility.value == "ineligible"
                    )
                ):
                    rejected.append(
                        RejectedOffer(
                            source_id=source.id,
                            company=source.company,
                            title=offer.title,
                            url=str(offer.url),
                            stage="location",
                            reasons=prefilter.reasons,
                            prefilter=prefilter,
                        )
                    )
                    continue
                offers.append(offer)
                accepted_for_source += 1
                if limit_per_source is not None and accepted_for_source >= limit_per_source:
                    break
            observation.selected_count = accepted_for_source
            if progress_callback:
                progress_callback(source.company, source_index, len(enabled_sources))
    offers.sort(
        key=lambda offer: (offer.company.casefold(), offer.title.casefold(), str(offer.url))
    )
    return CollectionResult(
        offers=offers, errors=errors, rejected=rejected, observations=observations
    )


def write_collection(result: CollectionResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
