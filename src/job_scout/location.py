"""Deterministic handling of the only hard MVP eligibility rule."""

from __future__ import annotations

import re

from .domain import JobOffer, LocationEligibility

WARSAW_MARKERS = ("warsaw", "warszawa")
POLAND_MARKERS = ("poland", "polska", "pl only", "within poland")
REMOTE_MARKERS = ("remote", "zdaln")


def classify_location(offer: JobOffer) -> LocationEligibility:
    if offer.remote_from_poland is True:
        return LocationEligibility.ELIGIBLE

    text = " ".join(offer.locations).casefold()
    if any(marker in text for marker in WARSAW_MARKERS):
        return LocationEligibility.ELIGIBLE
    if any(marker in text for marker in REMOTE_MARKERS) and any(
        marker in text for marker in POLAND_MARKERS
    ):
        return LocationEligibility.ELIGIBLE

    if offer.remote_from_poland is None and not text.strip():
        return LocationEligibility.UNKNOWN
    if offer.remote_from_poland is None and any(marker in text for marker in REMOTE_MARKERS):
        return LocationEligibility.UNKNOWN
    if re.search(r"\b(remote|hybrid)\b", text) and not any(
        marker in text for marker in POLAND_MARKERS + WARSAW_MARKERS
    ):
        return LocationEligibility.UNKNOWN
    return LocationEligibility.INELIGIBLE
