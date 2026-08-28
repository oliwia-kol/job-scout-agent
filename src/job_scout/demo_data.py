"""Validated, reproducible inputs for the local AI demo."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob, build_analysis_text
from .collector import CollectionResult
from .domain import CandidateProfile


class FrozenOfferReference(BaseModel):
    company: str
    title: str
    raw_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class FrozenDemoManifest(BaseModel):
    sample_id: str
    source_snapshot: Path
    offers: list[FrozenOfferReference] = Field(min_length=1)


def load_candidate_profile(path: Path) -> CandidateProfile:
    return CandidateProfile.model_validate_json(path.read_text(encoding="utf-8"))


def normalize_model_text(value: str) -> str:
    """Remove literal/encoded markup and collapse whitespace before local inference."""
    decoded = value
    for _ in range(3):
        expanded = unescape(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    text = BeautifulSoup(decoded, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def load_frozen_demo_offers(manifest_path: Path, project_root: Path) -> list[CleanJob]:
    manifest = FrozenDemoManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    collection_path = project_root / manifest.source_snapshot
    collection = CollectionResult.model_validate_json(collection_path.read_text(encoding="utf-8"))
    by_identity = {(offer.company, offer.title): offer for offer in collection.offers}
    selected: list[CleanJob] = []
    for reference in manifest.offers:
        key = (reference.company, reference.title)
        offer = by_identity.get(key)
        if offer is None:
            raise ValueError(f"frozen offer missing from source snapshot: {key!r}")
        if offer.raw_sha256 != reference.raw_sha256:
            raise ValueError(f"frozen offer hash changed: {key!r}")
        selected.append(
            offer.model_copy(
                update={
                    "analysis_text": normalize_model_text(build_analysis_text(offer.description)[0])
                }
            )
        )
    return selected


def build_model_input_snapshot(offer: CleanJob) -> dict:
    """Return the immutable fields stored for each evaluation-run item."""
    return {
        "company": offer.company,
        "title": offer.title,
        "url": str(offer.url),
        "analysis_text": normalize_model_text(offer.analysis_text),
        "supplemental_info": offer.supplemental_info.model_dump(mode="json"),
        "raw_sha256": offer.raw_sha256,
    }
