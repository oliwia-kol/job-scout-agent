"""Unknown-safe decisions for every user-facing job condition.

Absence of data is deliberately not treated as a mismatch.  This module is small on
purpose: extractors and LLMs may return ``unknown`` for any field, while only the
deterministic policy decides whether an offer can be hidden.
"""

from __future__ import annotations

from enum import StrEnum


class ConditionState(StrEnum):
    MATCH = "match"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class ConditionPriority(StrEnum):
    PREFERENCE = "preference"
    IMPORTANT = "important"
    REQUIRED = "required"


class VisibilityDecision(StrEnum):
    SHOW = "show"
    HIDE = "hide"


def decide_visibility(
    *,
    priority: ConditionPriority,
    state: ConditionState,
) -> VisibilityDecision:
    """Hide only an explicitly contradictory required condition.

    ``UNKNOWN`` covers missing listing fields, scraper ``N/A``, low-confidence
    extraction and an ambiguous statement. It must always remain visible.
    """
    if priority == ConditionPriority.REQUIRED and state == ConditionState.CONFLICT:
        return VisibilityDecision.HIDE
    return VisibilityDecision.SHOW


def user_explanation(
    *,
    priority: ConditionPriority,
    state: ConditionState,
) -> str:
    """Short, consistent product copy for a condition outcome."""
    if state == ConditionState.MATCH:
        return "Potwierdzona zgodność"
    if state == ConditionState.CONFLICT and priority == ConditionPriority.REQUIRED:
        return "Potwierdzona niezgodność — oferta ukryta"
    if state == ConditionState.CONFLICT:
        return "Potwierdzona niezgodność — oferta pozostaje widoczna"
    return "Nie wiadomo — oferta pozostaje widoczna"
