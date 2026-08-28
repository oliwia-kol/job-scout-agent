"""Strict separation and coverage checks for Golden Dataset v1."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

DECISIONS = {"apply", "consider", "reject"}
SCORE_FIELDS = (
    "role_direction_fit",
    "candidate_fit",
    "current_cv_fit",
    "tailored_cv_potential",
)
FORBIDDEN_VALIDATION_KEYS = {
    "expected",
    "decision",
    "dominant_reason",
    "candidate_fit",
    "current_cv_fit",
    "tailored_cv_potential",
    "role_direction_fit",
    "preference_fit",
}


def _round_score(value: float) -> int:
    return min(5, max(0, math.floor(value + 0.5)))


def calculate_preference_fit(offer: dict) -> dict:
    """Apply the Golden rubric deterministically to structured offer conditions."""
    conditions = offer.get("conditions") or {}
    contract_text = str(conditions.get("contract") or "").strip()
    contract_folded = contract_text.casefold()

    salary_key, salary_value = next(
        (
            (str(key), value)
            for key, value in conditions.items()
            if str(key).startswith("salary")
        ),
        ("", None),
    )
    salary_score: int | None = None
    salary_evidence = "unknown"
    if isinstance(salary_value, list) and len(salary_value) == 2:
        midpoint = (float(salary_value[0]) + float(salary_value[1])) / 2
        if "b2b" in salary_key:
            raw_salary_score = 5 * (midpoint - 11250) / 5000
        else:
            raw_salary_score = 5 * (midpoint - 10000) / 8000
        salary_score = _round_score(raw_salary_score)
        salary_evidence = f"{salary_key}: midpoint={midpoint:g}, score={salary_score}"

    remote_text = str(conditions.get("remote") or "").strip()
    remote_folded = remote_text.casefold()
    remote_score: int | None
    if not remote_text or any(word in remote_folded for word in ("unknown", "not stated")):
        remote_score = None
        remote_evidence = "unknown"
    elif "fully remote" in remote_folded:
        remote_score = 5
        remote_evidence = remote_text
    elif "two office days per month" in remote_folded:
        remote_score = 4
        remote_evidence = remote_text
    elif "one office day per week" in remote_folded:
        remote_score = 1
        remote_evidence = remote_text
    elif "office days per week" in remote_folded or "on-site" in remote_folded:
        remote_score = 0
        remote_evidence = remote_text
    else:
        remote_score = None
        remote_evidence = "unknown"

    contract_score: int | None
    hard_conflicts: list[str] = []
    if not contract_text or "unknown" in contract_folded:
        contract_score = None
        contract_evidence = "unknown"
    elif "uop" in contract_folded and "full time" in contract_folded:
        contract_score = 5
        contract_evidence = contract_text
    elif "b2b" in contract_folded:
        paid_leave = "paid" in contract_folded and "without paid" not in contract_folded
        notice = "notice" in contract_folded and not (
            "without notice" in contract_folded
            or "without paid leave or notice" in contract_folded
        )
        protections = sum(
            (paid_leave, notice, "medical" in contract_folded, "bonus" in contract_folded)
        )
        contract_score = max(2, min(5, 1 + protections))
        contract_evidence = contract_text
        if protections == 0 or (not paid_leave and not notice):
            hard_conflicts.append("B2B lacks employment-like protections")
    elif "project contract" in contract_folded or "one project" in contract_folded:
        contract_score = 0
        contract_evidence = contract_text
        hard_conflicts.append("unstable project-only contract")
    else:
        contract_score = None
        contract_evidence = "unknown"

    if remote_score == 0:
        hard_conflicts.append("frequent mandatory office presence")

    components = {
        "salary": {"score": salary_score, "evidence": salary_evidence},
        "remote": {"score": remote_score, "evidence": remote_evidence},
        "contract": {"score": contract_score, "evidence": contract_evidence},
    }
    weights = {"salary": 0.35, "remote": 0.4, "contract": 0.25}
    coverage = sum(
        weights[name] for name, component in components.items() if component["score"] is not None
    )
    weighted = sum(
        weights[name] * component["score"]
        for name, component in components.items()
        if component["score"] is not None
    )
    score = _round_score(weighted / coverage) if coverage else None
    return {
        "score": score,
        "coverage": round(coverage, 2),
        **components,
        "hard_conflicts": hard_conflicts,
    }


def _load(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Golden v1 artifact is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Golden v1 artifact is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Golden v1 artifact must be an object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_forbidden_keys(value: object, *, path: str = "root") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}"
            if key in FORBIDDEN_VALIDATION_KEYS:
                hits.append(current)
            hits.extend(_find_forbidden_keys(item, path=current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_find_forbidden_keys(item, path=f"{path}[{index}]"))
    return hits


def _validate_expected(case_id: str, expected: dict) -> None:
    if expected.get("decision") not in DECISIONS:
        raise ValueError(f"{case_id}: invalid decision")
    for field in SCORE_FIELDS:
        score = expected.get(field)
        if not isinstance(score, int) or not 1 <= score <= 5:
            raise ValueError(f"{case_id}: {field} must be an integer 1-5")
    preference = expected.get("preference_fit")
    if preference is not None and (not isinstance(preference, int) or not 0 <= preference <= 5):
        raise ValueError(f"{case_id}: preference_fit must be null or an integer 0-5")
    coverage = expected.get("preference_coverage")
    if not isinstance(coverage, (int, float)) or not 0 <= coverage <= 1:
        raise ValueError(f"{case_id}: preference_coverage must be between 0 and 1")
    if not str(expected.get("dominant_reason") or "").strip():
        raise ValueError(f"{case_id}: dominant_reason is required")
    conflicts = set(expected.get("conflicts") or [])
    unknowns = set(expected.get("unknowns") or [])
    if conflicts.intersection(unknowns):
        raise ValueError(f"{case_id}: an item cannot be both conflict and unknown")


def validate_golden_v1(
    *,
    calibration_path: Path,
    validation_inputs_path: Path,
    validation_key_path: Path,
    prefilter_path: Path,
    instruction_path: Path,
    profile_context_path: Path,
) -> dict:
    """Prove balance, label isolation, referential integrity and instruction hygiene."""
    calibration = _load(calibration_path)
    validation = _load(validation_inputs_path)
    key = _load(validation_key_path)
    prefilter = _load(prefilter_path)
    profile_context = _load(profile_context_path)
    calibration_cases = calibration.get("cases")
    validation_cases = validation.get("cases")
    key_cases = key.get("cases")
    prefilter_cases = prefilter.get("cases")
    if not isinstance(calibration_cases, list) or len(calibration_cases) != 18:
        raise ValueError("Golden v1 calibration must contain exactly 18 cases")
    if not isinstance(validation_cases, list) or len(validation_cases) != 18:
        raise ValueError("Golden v1 validation inputs must contain exactly 18 cases")
    if not isinstance(key_cases, list) or len(key_cases) != 18:
        raise ValueError("Golden v1 private validation key must contain exactly 18 cases")
    if not isinstance(prefilter_cases, list) or len(prefilter_cases) != 8:
        raise ValueError("Golden v1 prefilter must contain exactly 8 cases")

    calibration_labels = {decision: 0 for decision in DECISIONS}
    calibration_ids: set[str] = set()
    tags: set[str] = set()
    for case in calibration_cases:
        case_id = str(case.get("case_id") or "")
        if not case_id or case_id in calibration_ids:
            raise ValueError("Golden v1 calibration case ids must be unique")
        calibration_ids.add(case_id)
        expected = case.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"{case_id}: expected label object is required")
        _validate_expected(case_id, expected)
        deterministic_preference = calculate_preference_fit(case.get("offer") or {})
        if expected.get("preference_fit") != deterministic_preference["score"]:
            raise ValueError(f"{case_id}: preference_fit contradicts deterministic rubric")
        if abs(
            float(expected.get("preference_coverage"))
            - deterministic_preference["coverage"]
        ) > 0.01:
            raise ValueError(f"{case_id}: preference_coverage contradicts rubric")
        calibration_labels[expected["decision"]] += 1
        tags.update(str(tag) for tag in case.get("tags") or [])
    if set(calibration_labels.values()) != {6}:
        raise ValueError("Golden v1 calibration must have 6 cases per decision class")

    forbidden = _find_forbidden_keys(validation)
    if forbidden:
        raise ValueError("Validation inputs leak labels at: " + ", ".join(forbidden[:5]))
    validation_ids = [str(case.get("case_id") or "") for case in validation_cases]
    validation_by_id = {str(case.get("case_id") or ""): case for case in validation_cases}
    if len(set(validation_ids)) != 18 or any(not case_id for case_id in validation_ids):
        raise ValueError("Golden v1 validation input ids must be unique and non-empty")
    if calibration_ids.intersection(validation_ids):
        raise ValueError("Calibration and validation case ids must be disjoint")
    for case in validation_cases:
        tags.update(str(tag) for tag in case.get("tags") or [])

    key_by_id = {str(case.get("case_id") or ""): case for case in key_cases}
    if set(key_by_id) != set(validation_ids):
        raise ValueError("Private answer-key ids must exactly match validation input ids")
    validation_labels = {decision: 0 for decision in DECISIONS}
    for case_id, expected in key_by_id.items():
        _validate_expected(case_id, expected)
        deterministic_preference = calculate_preference_fit(
            validation_by_id[case_id].get("offer") or {}
        )
        if expected.get("preference_fit") != deterministic_preference["score"]:
            raise ValueError(
                f"{case_id}: private preference_fit contradicts deterministic rubric"
            )
        if abs(
            float(expected.get("preference_coverage"))
            - deterministic_preference["coverage"]
        ) > 0.01:
            raise ValueError(
                f"{case_id}: private preference_coverage contradicts rubric"
            )
        validation_labels[expected["decision"]] += 1
        if "offer" in expected or "responsibilities" in expected:
            raise ValueError(f"{case_id}: private key must not duplicate validation inputs")
    if set(validation_labels.values()) != {6}:
        raise ValueError("Golden v1 validation must have 6 cases per decision class")

    statuses = [case.get("expected_scope_status") for case in prefilter_cases]
    if not {"included", "excluded"}.issubset(statuses):
        raise ValueError("Golden v1 prefilter must contain included and excluded cases")
    if len(tags) < 24:
        raise ValueError("Golden v1 synthetic benchmark has insufficient scenario diversity")

    career_facts = profile_context.get("career_facts")
    cv_evidence = profile_context.get("current_cv_evidence")
    if not isinstance(career_facts, list) or not career_facts:
        raise ValueError("Golden v1 profile context requires career_facts")
    fact_ids = [str(fact.get("fact_id") or "") for fact in career_facts]
    if any(not fact_id for fact_id in fact_ids) or len(set(fact_ids)) != len(fact_ids):
        raise ValueError("Golden v1 profile context fact ids must be unique and non-empty")
    if not isinstance(cv_evidence, list) or not cv_evidence:
        raise ValueError("Golden v1 profile context requires current_cv_evidence")

    try:
        instruction = instruction_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Golden v1 model instruction is missing: {instruction_path}") from exc
    if "syn-val-" in instruction or "validation.answer-key" in instruction:
        raise ValueError("Model instruction leaks validation identifiers or answer-key location")

    return {
        "calibration_cases": len(calibration_cases),
        "validation_cases": len(validation_cases),
        "prefilter_cases": len(prefilter_cases),
        "unique_tags": len(tags),
        "calibration_labels": calibration_labels,
        "validation_labels": validation_labels,
        "validation_inputs_sha256": _sha256(validation_inputs_path),
        "validation_key_sha256": _sha256(validation_key_path),
        "instruction_sha256": _sha256(instruction_path),
        "profile_context_sha256": _sha256(profile_context_path),
    }


def _actual_score(result: dict, field: str) -> int | None:
    value = result.get(field)
    if isinstance(value, dict):
        value = value.get("score")
    return value if isinstance(value, int) else None


def _macro_f1(expected: list[str], actual: list[str]) -> tuple[float, dict[str, float]]:
    by_class: dict[str, float] = {}
    for label in sorted(DECISIONS):
        true_positive = sum(e == label and a == label for e, a in zip(expected, actual))
        false_positive = sum(e != label and a == label for e, a in zip(expected, actual))
        false_negative = sum(e == label and a != label for e, a in zip(expected, actual))
        precision_denominator = true_positive + false_positive
        recall_denominator = true_positive + false_negative
        precision = true_positive / precision_denominator if precision_denominator else 0
        recall = true_positive / recall_denominator if recall_denominator else 0
        by_class[label] = (
            2 * precision * recall / (precision + recall) if precision + recall else 0
        )
    return sum(by_class.values()) / len(by_class), by_class


def _score_result_set(
    *,
    results_payload: dict,
    input_by_id: dict[str, dict],
    expected_by_id: dict[str, dict],
    report_version: str,
    frozen_artifacts: dict[str, str],
) -> dict:
    results = results_payload.get("results")
    if not isinstance(results, list):
        raise ValueError("Golden v1 model results must contain a results list")
    actual_by_id = {str(case.get("case_id") or ""): case for case in results}
    if set(actual_by_id) != set(input_by_id) or set(expected_by_id) != set(input_by_id):
        raise ValueError("Result ids must exactly match frozen benchmark input ids")

    expected_labels: list[str] = []
    actual_labels: list[str] = []
    score_errors: dict[str, list[int]] = defaultdict(list)
    mismatches = []
    grounded_cases = 0
    for case_id in input_by_id:
        expected = expected_by_id[case_id]
        actual = actual_by_id[case_id]
        expected_label = expected["decision"]
        actual_label = str(actual.get("decision") or "")
        if actual_label not in DECISIONS:
            raise ValueError(f"{case_id}: model result has invalid decision")
        expected_labels.append(expected_label)
        actual_labels.append(actual_label)
        score_differences = {}
        for field in SCORE_FIELDS:
            actual_value = _actual_score(actual, field)
            if actual_value is None or not 1 <= actual_value <= 5:
                raise ValueError(f"{case_id}: model result lacks valid {field}")
            difference = abs(actual_value - expected[field])
            score_errors[field].append(difference)
            score_differences[field] = difference

        offer_text = json.dumps(input_by_id[case_id]["offer"], ensure_ascii=False).casefold()
        quotes = []
        for field in ("role_direction_fit", "candidate_fit"):
            value = actual.get(field)
            if isinstance(value, dict):
                quotes.extend(str(quote).strip() for quote in value.get("offer_quotes") or [])
        grounded = bool(quotes) and all(quote.casefold() in offer_text for quote in quotes)
        grounded_cases += int(grounded)
        if actual_label != expected_label or any(score_differences.values()) or not grounded:
            mismatches.append(
                {
                    "case_id": case_id,
                    "expected_decision": expected_label,
                    "actual_decision": actual_label,
                    "score_absolute_errors": score_differences,
                    "evidence_grounded": grounded,
                    "expected_dominant_reason": expected["dominant_reason"],
                    "actual_dominant_reason": actual.get("dominant_reason"),
                }
            )

    macro_f1, f1_by_class = _macro_f1(expected_labels, actual_labels)
    recall_by_class = {
        label: (
            sum(e == label and a == label for e, a in zip(expected_labels, actual_labels))
            / sum(e == label for e in expected_labels)
            if sum(e == label for e in expected_labels)
            else 0
        )
        for label in sorted(DECISIONS)
    }
    return {
        "schema_version": report_version,
        "cases": len(expected_labels),
        "decision_accuracy": sum(e == a for e, a in zip(expected_labels, actual_labels))
        / len(expected_labels),
        "macro_f1": macro_f1,
        "f1_by_class": f1_by_class,
        "recall_by_class": recall_by_class,
        "false_apply": sum(
            e == "reject" and a == "apply" for e, a in zip(expected_labels, actual_labels)
        ),
        "false_reject": sum(
            e == "apply" and a == "reject" for e, a in zip(expected_labels, actual_labels)
        ),
        "score_mae": {
            field: sum(errors) / len(errors) for field, errors in score_errors.items()
        },
        "grounded_evidence_rate": grounded_cases / len(expected_labels),
        "passed_working_thresholds": (
            macro_f1 >= 0.8
            and recall_by_class["apply"] >= 0.83
            and recall_by_class["reject"] >= 0.83
            and not any(
                e == "reject" and a == "apply"
                for e, a in zip(expected_labels, actual_labels)
            )
            and all(sum(errors) / len(errors) <= 0.6 for errors in score_errors.values())
            and grounded_cases == len(expected_labels)
        ),
        "mismatches": mismatches,
        "frozen_artifacts": frozen_artifacts,
    }


def score_golden_v1_results(
    *, results_path: Path, validation_inputs_path: Path, validation_key_path: Path
) -> dict:
    """Score frozen model outputs after inference; the key is opened only here."""
    results_payload = _load(results_path)
    validation = _load(validation_inputs_path)
    key = _load(validation_key_path)
    input_by_id = {case["case_id"]: case for case in validation.get("cases") or []}
    expected_by_id = {case["case_id"]: case for case in key.get("cases") or []}
    return _score_result_set(
        results_payload=results_payload,
        input_by_id=input_by_id,
        expected_by_id=expected_by_id,
        report_version="golden-v1-validation-report",
        frozen_artifacts={
            "inputs_sha256": _sha256(validation_inputs_path),
            "key_sha256": _sha256(validation_key_path),
            "results_sha256": _sha256(results_path),
        },
    )


def score_golden_v1_calibration_results(
    *, results_path: Path, calibration_path: Path, allow_partial: bool = False
) -> dict:
    """Score calibration outputs whose labels are public and safe to inspect."""
    results_payload = _load(results_path)
    calibration = _load(calibration_path)
    cases = calibration.get("cases") or []
    input_by_id = {case["case_id"]: case for case in cases}
    expected_by_id = {case["case_id"]: case["expected"] for case in cases}
    if allow_partial:
        result_ids = {
            str(result.get("case_id") or "")
            for result in results_payload.get("results") or []
        }
        if not result_ids or not result_ids.issubset(input_by_id):
            raise ValueError("Partial calibration result ids must be a non-empty known subset")
        input_by_id = {case_id: input_by_id[case_id] for case_id in result_ids}
        expected_by_id = {case_id: expected_by_id[case_id] for case_id in result_ids}
    return _score_result_set(
        results_payload=results_payload,
        input_by_id=input_by_id,
        expected_by_id=expected_by_id,
        report_version="golden-v1-calibration-report",
        frozen_artifacts={
            "calibration_sha256": _sha256(calibration_path),
            "results_sha256": _sha256(results_path),
        },
    )
