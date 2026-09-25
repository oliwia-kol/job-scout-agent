"""Optional Gemini role-fit call; sends only approved profile facts and offer excerpts."""

from __future__ import annotations

import os
import re
import time

import httpx
from dotenv import load_dotenv

from .ats_scrapers import CleanJob
from .domain import CandidateProfile
from .role_fit import SYSTEM_PROMPT, RoleFitAnswer, constrained_schema, model_input, validate_answer

MODEL = "gemini-3.8-flash"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def _redact_contacts(value: str) -> str:
    value = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email removed]", value, flags=re.I
    )
    return re.sub(r"(?<!\w)(?:\+\d{1,3}[ -]?)?(?:\d[ -]?){9,14}(?!\w)", "[phone removed]", value)


def evaluate_gemini(
    offer: CleanJob, profile: CandidateProfile, *, client: httpx.Client | None = None
) -> tuple[RoleFitAnswer, dict[str, str], int]:
    load_dotenv()
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    prompt, catalog = model_input(offer, profile)
    prompt = _redact_contacts(prompt)
    request = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 700,
            "responseMimeType": "application/json",
            "responseSchema": constrained_schema(catalog, profile),
            "thinkingConfig": {"thinkingLevel": "LOW"},
        },
    }
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=90)
    started = time.perf_counter()
    try:
        response = client.post(ENDPOINT, headers={"x-goog-api-key": key}, json=request)
        if response.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:500]}")
        response.raise_for_status()
        payload = response.json()
        parts = payload["candidates"][0]["content"]["parts"]
        answer = RoleFitAnswer.model_validate_json("".join(part.get("text", "") for part in parts))
        validate_answer(answer, catalog, profile)
        return answer, catalog, round((time.perf_counter() - started) * 1000)
    finally:
        if owns_client:
            client.close()
