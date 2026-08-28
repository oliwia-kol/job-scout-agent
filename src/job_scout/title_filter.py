"""Transparent title gate applied before downloading job descriptions."""

from __future__ import annotations

import re

TITLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "AI": re.compile(r"\bAI\b|artificial intelligence", re.IGNORECASE),
    "GenAI": re.compile(r"\bgen(?:erative)?\s*ai\b", re.IGNORECASE),
    "LLM": re.compile(r"\bLLMs?\b|large language model", re.IGNORECASE),
    "ML": re.compile(r"\bML\b|machine learning", re.IGNORECASE),
    "Data Scientist": re.compile(r"\bdata scientist\b", re.IGNORECASE),
    "Applied Scientist": re.compile(r"\bapplied scientist\b", re.IGNORECASE),
    "Research Engineer": re.compile(r"\bresearch engineer\b", re.IGNORECASE),
    "NLP": re.compile(r"\bNLP\b|natural language processing", re.IGNORECASE),
    "Computer Vision": re.compile(r"\bcomputer vision\b", re.IGNORECASE),
    "Agentic": re.compile(r"\bagentic\b|\bAI agents?\b", re.IGNORECASE),
    "Automation Engineer": re.compile(r"\bautomation engineer\b", re.IGNORECASE),
}


def match_title(title: str) -> list[str]:
    return [label for label, pattern in TITLE_PATTERNS.items() if pattern.search(title)]


def is_target_title(title: str) -> bool:
    return bool(match_title(title))
