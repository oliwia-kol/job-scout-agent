"""Validation for the manually curated, versioned offer reference set."""

from __future__ import annotations

import json
from pathlib import Path

from .storage import get_offer, list_offer_section_evidence

REQUIRED_DECISION_FIELDS = {
    "label",
    "reason",
    "confirmed_conflicts",
    "unknowns",
    "positives",
}
REQUIRED_EXPECTED_FIELDS = {"responsibilities", "requirements", "conditions", "source_quotes"}
REQUIRED_ASSESSMENT_FIELDS = {
    "candidate_fit",
    "current_cv_fit",
    "tailored_cv_potential",
    "preference_fit",
    "role_direction_fit",
}
# The default benchmark stays inside the user's target role family. Managerial,
# product, consulting and presales roles are excluded even when "AI" is in the title.
DEFAULT_QUIZ_OFFER_IDS = (1, 5, 10, 12, 13, 14, 15, 16, 17, 18, 19, 26)


def load_golden_dataset(path: Path) -> dict:
    """Load and validate the v0 contract without silently accepting weak fixtures."""
    try:
        dataset = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"golden dataset is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"golden dataset is not valid JSON: {exc}") from exc
    if not isinstance(dataset, dict) or dataset.get("dataset_version") != "golden-v0":
        raise ValueError("dataset_version must be golden-v0")
    policy = dataset.get("split_policy")
    if policy != {"calibration_ratio": 0.7, "validation_ratio": 0.3}:
        raise ValueError("split_policy must be exactly calibration_ratio=0.7, validation_ratio=0.3")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not 12 <= len(cases) <= 15:
        raise ValueError("golden-v0 must contain 12 to 15 cases")
    identifiers: set[str] = set()
    split_counts = {"calibration": 0, "validation": 0}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each golden case must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or case_id in identifiers:
            raise ValueError("each case needs a unique case_id")
        identifiers.add(case_id)
        split = case.get("split")
        if split not in split_counts:
            raise ValueError(f"{case_id}: split must be calibration or validation")
        split_counts[split] += 1
        offer = case.get("offer")
        if not isinstance(offer, dict) or not all(
            offer.get(field) for field in ("offer_id", "source_url", "content_hash")
        ) or not isinstance(offer.get("version"), int):
            raise ValueError(f"{case_id}: offer needs id, version, source URL and content hash")
        expected = case.get("expected")
        if not isinstance(expected, dict) or not REQUIRED_EXPECTED_FIELDS.issubset(expected):
            raise ValueError(f"{case_id}: expected fields are incomplete")
        if not isinstance(expected["source_quotes"], list) or not expected["source_quotes"]:
            raise ValueError(f"{case_id}: at least one source quote is required")
        assessment = case.get("assessment")
        if not isinstance(assessment, dict) or not REQUIRED_ASSESSMENT_FIELDS.issubset(
            assessment
        ):
            raise ValueError(f"{case_id}: assessment fields are incomplete")
        for field in (
            "candidate_fit",
            "current_cv_fit",
            "tailored_cv_potential",
            "role_direction_fit",
        ):
            dimension = assessment[field]
            if not isinstance(dimension, dict) or dimension.get("score") not in range(1, 6):
                raise ValueError(f"{case_id}: {field} score must be an integer from 1 to 5")
            if not str(dimension.get("comment") or "").strip():
                raise ValueError(f"{case_id}: {field} comment is required")
        preference = assessment["preference_fit"]
        if not isinstance(preference, dict) or preference.get("status") not in {
            "scored",
            "partial",
            "insufficient_data",
        }:
            raise ValueError(f"{case_id}: invalid preference_fit status")
        decision = case.get("decision")
        if not isinstance(decision, dict) or not REQUIRED_DECISION_FIELDS.issubset(decision):
            raise ValueError(f"{case_id}: decision fields are incomplete")
        if decision.get("label") not in {"apply", "consider", "reject"}:
            raise ValueError(f"{case_id}: invalid decision label")
        if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
            raise ValueError(f"{case_id}: decision reason is required")
    validation_share = split_counts["validation"] / len(cases)
    if not 0.25 <= validation_share <= 0.35:
        raise ValueError("validation split must be approximately 30% of cases")
    return dataset


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def verify_golden_extraction(dataset_path: Path, database_path: Path) -> dict[str, int]:
    """Verify deterministic evidence against frozen human-authored expectations.

    This intentionally does not score decisions. It proves that the source version,
    citations and fields needed for a decision still agree with Golden dataset v0.
    """
    dataset = load_golden_dataset(dataset_path)
    errors: list[str] = []
    for case in dataset["cases"]:
        case_id = case["case_id"]
        try:
            offer_id = int(case["offer"]["offer_id"])
        except (TypeError, ValueError):
            errors.append(f"{case_id}: offer_id must be an existing SQLite integer id")
            continue
        offer = get_offer(database_path, offer_id)
        if not offer:
            errors.append(f"{case_id}: offer {offer_id} is missing")
            continue
        current_hash = offer.get("current_content_sha256")
        if current_hash != case["offer"]["content_hash"]:
            errors.append(f"{case_id}: offer content hash changed")
            continue
        evidence = list_offer_section_evidence(database_path, offer_id, current_hash)
        evidence_text = {
            section: _normalise(
                " ".join(
                    entry["source_quote"]
                    for entry in evidence
                    if entry["section_kind"] == section
                )
            )
            for section in {"responsibilities", "requirements", "work_conditions", "compensation"}
        }
        source_text = _normalise(
            str(offer["offer"].get("description") or offer["offer"].get("analysis_text") or "")
        )
        for quote in case["expected"]["source_quotes"]:
            if _normalise(str(quote)) not in source_text:
                errors.append(f"{case_id}: source quote is absent from the frozen offer")
        for section, expected_key in (
            ("responsibilities", "responsibilities"),
            ("requirements", "requirements"),
        ):
            for expected in case["expected"][expected_key]:
                if _normalise(str(expected)) not in evidence_text[section]:
                    errors.append(f"{case_id}: {section} expectation is not covered by evidence")
        for _, expected in case["expected"]["conditions"].items():
            if _normalise(str(expected)) not in (
                evidence_text["work_conditions"] + " " + evidence_text["compensation"]
            ):
                errors.append(f"{case_id}: condition expectation is not covered by evidence")
    if errors:
        raise ValueError("Golden extraction verification failed:\n- " + "\n- ".join(errors))
    cases = dataset["cases"]
    return {
        "cases_verified": len(cases),
        "validation_cases": sum(case["split"] == "validation" for case in cases),
    }


def build_golden_quiz_pack(database_path: Path, offer_ids: list[int]) -> dict:
    """Prepare local, cited material for a human decision quiz without making decisions."""
    if not 12 <= len(offer_ids) <= 15 or len(set(offer_ids)) != len(offer_ids):
        raise ValueError("quiz pack needs 12 to 15 unique offer ids")
    cases = []
    calibration_count = round(len(offer_ids) * 0.7)
    for number, offer_id in enumerate(offer_ids, start=1):
        item = get_offer(database_path, offer_id)
        if not item or item.get("availability_status") != "active":
            raise ValueError(f"offer {offer_id} is not an active stored offer")
        evidence = list_offer_section_evidence(
            database_path, offer_id, item["current_content_sha256"]
        )
        sections: dict[str, list[dict]] = {}
        for entry in evidence:
            sections.setdefault(entry["section_kind"], []).append(
                {
                    "category": entry["category"],
                    "text": entry["value"].get("text", ""),
                    "source_quote": entry["source_quote"],
                }
            )
        cases.append(
            {
                "case_id": f"golden-v0-{number:02d}",
                "split": "calibration" if number <= calibration_count else "validation",
                "offer": {
                    "offer_id": str(offer_id),
                    "version": len(item.get("versions") or []) or 1,
                    "company": item["company"],
                    "title": item["title"],
                    "source_url": item["job_url"],
                    "content_hash": item["current_content_sha256"],
                },
                "observed_evidence": sections,
                "unknown_sections": [
                    section
                    for section in (
                        "responsibilities",
                        "requirements",
                        "work_conditions",
                        "compensation",
                        "travel",
                    )
                    if section not in sections
                ],
                "quiz": {
                    "decision_options": ["apply", "consider", "reject"],
                    "questions": [
                        "Jak asystent ocenia Role Direction Fit 1–5 na podstawie "
                        "dominujących obowiązków?",
                        "Jaka jest Twoja decyzja: apply, consider czy reject?",
                        "Jaki jest jeden konkretny powód decyzji?",
                        "Jakie potwierdzone konflikty widzisz?",
                        "Jakie informacje pozostają unknown?",
                        "Jakie elementy są pozytywne?",
                    ],
                },
            }
        )
    return {
        "quiz_version": "golden-v0-quiz-1",
        "dataset_version_target": "golden-v0",
        "split_policy": {"calibration_ratio": 0.7, "validation_ratio": 0.3},
        "decision_notice": "Decyzje zapisuje wyłącznie użytkowniczka podczas quizu.",
        "cases": cases,
    }
