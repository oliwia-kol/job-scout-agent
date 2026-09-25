"""Cheap deterministic filtering before any local LLM inference."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .ats_scrapers import CleanJob
from .domain import LocationEligibility
from .title_filter import match_title

AI_PATTERNS: dict[str, int] = {
    r"\b(applied ai|generative ai|genai)\b": 4,
    r"\b(ai agent|ai agents|agentic|multi-agent)\b": 4,
    r"\b(llm|large language model|rag|retrieval.augmented)\b": 3,
    r"\b(ai automation|intelligent automation|prompt orchestration)\b": 4,
    r"\b(model evaluation|llm evaluation|genai evaluation|guardrails?)\b": 4,
    r"\b(machine learning|ml engineer|data scientist|ai engineer)\b": 2,
    r"\b(nlp|computer vision|deep learning|pytorch|tensorflow)\b": 1,
}
NON_TARGET_TITLE = re.compile(
    r"\b(marketing|social media|sales|account manager|legal|counsel|audit|"
    r"partnerships?|recruiter|talent acquisition|hr)\b",
    re.IGNORECASE,
)
WARSAW = ("warsaw", "warszawa", "mokotów", "mokotow", "wola", "żoliborz", "zoliborz")
POLAND = ("poland", "polska")
OTHER_POLISH_CITIES = (
    "krakow",
    "kraków",
    "wroclaw",
    "wrocław",
    "poznan",
    "poznań",
    "gdansk",
    "gdańsk",
    "lodz",
    "łódź",
    "katowice",
    "lublin",
)
REMOTE = ("remote", "zdaln", "work from home")
GLOBAL_REMOTE = ("globally", "worldwide", "anywhere", "global team")
FOREIGN_ONLY = (
    "united states",
    "usa",
    "london",
    "japan",
    "france",
    "germany",
    "spain",
    "united kingdom",
    "cyprus",
)


class PrefilterResult(BaseModel):
    passed: bool
    location_eligibility: LocationEligibility
    relevance_score: int = Field(ge=0)
    matched_signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def infer_location(offer: CleanJob) -> LocationEligibility:
    location_text = " ".join(offer.locations).casefold()
    evidence = f"{location_text} {offer.analysis_text[-1200:].casefold()}"
    if re.search(
        r"\brelocat(?:ion|e)\s+to\s+(?:cyprus|germany|france|spain|united kingdom|usa)\b",
        offer.title,
        re.I,
    ):
        return LocationEligibility.INELIGIBLE
    if any(marker in location_text for marker in REMOTE) and any(
        marker in location_text for marker in POLAND
    ):
        return LocationEligibility.ELIGIBLE
    if any(marker in location_text for marker in WARSAW):
        return LocationEligibility.ELIGIBLE
    if any(marker in location_text for marker in OTHER_POLISH_CITIES):
        return LocationEligibility.INELIGIBLE
    if location_text and any(marker in location_text for marker in FOREIGN_ONLY):
        if not any(marker in location_text for marker in POLAND):
            return LocationEligibility.INELIGIBLE
    if any(marker in evidence for marker in WARSAW):
        return LocationEligibility.ELIGIBLE
    if any(marker in evidence for marker in REMOTE) and any(
        marker in evidence for marker in POLAND
    ):
        return LocationEligibility.ELIGIBLE
    if any(marker in evidence for marker in REMOTE) and any(
        marker in evidence for marker in GLOBAL_REMOTE
    ):
        return LocationEligibility.ELIGIBLE
    if any(marker in evidence for marker in REMOTE):
        return LocationEligibility.UNKNOWN
    return LocationEligibility.UNKNOWN


def evaluate_prefilter(offer: CleanJob) -> PrefilterResult:
    body = offer.analysis_text.casefold()
    title = offer.title.casefold()
    score = 0
    signals: list[str] = []
    for pattern, weight in AI_PATTERNS.items():
        body_hits = len(re.findall(pattern, body, flags=re.IGNORECASE))
        title_hit = bool(re.search(pattern, title, flags=re.IGNORECASE))
        if body_hits or title_hit:
            score += min(body_hits, 3) * weight + (weight if title_hit else 0)
            signals.append(pattern.replace(r"\b", ""))

    reasons: list[str] = []
    title_signals = match_title(offer.title)
    signals = title_signals + signals
    if NON_TARGET_TITLE.search(title):
        score = max(0, score - 5)
        reasons.append("non-target role title")
    location = infer_location(offer)
    if location == LocationEligibility.INELIGIBLE:
        reasons.append("location outside Poland/Warsaw and not remote")
    if not title_signals:
        reasons.append("title does not match the configured AI/ML role keywords")
    passed = bool(title_signals) and location != LocationEligibility.INELIGIBLE
    return PrefilterResult(
        passed=passed,
        location_eligibility=location,
        relevance_score=score,
        matched_signals=signals,
        reasons=reasons,
    )
