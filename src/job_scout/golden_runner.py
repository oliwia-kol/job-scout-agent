"""Deterministic local-model runner for Golden Dataset v1."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .golden_v1 import calculate_preference_fit
from .local_llm import LocalLlmClient


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfferDimension(_StrictModel):
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=240)
    offer_quotes: list[str] = Field(max_length=2)


class RoleDirectionDimension(OfferDimension):
    core_family: Literal[
        "agentic_applied_ai",
        "ai_data_science",
        "ai_validation_risk",
        "external_deployment",
        "retrieval_data",
        "ai_software_fullstack",
        "ml_llmops_platform",
        "classical_ml",
        "presales_non_build",
        "model_research",
    ]
    core_certainty: Literal["confirmed", "ambiguous"]


class CandidateDimension(OfferDimension):
    profile_fact_ids: list[str] = Field(max_length=4)


class CvDimension(_StrictModel):
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=240)
    cv_quotes: list[str] = Field(max_length=3)


class TailoredCvDimension(_StrictModel):
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=240)
    profile_fact_ids: list[str] = Field(max_length=4)


class GoldenCoreEvaluation(_StrictModel):
    scope_status: Literal["included", "excluded"]
    scope_reason: str = Field(min_length=1, max_length=240)
    role_direction_fit: RoleDirectionDimension
    candidate_fit: CandidateDimension
    current_cv_fit: CvDimension
    tailored_cv_potential: TailoredCvDimension
    preference_fit: dict | None = Field(default=None, exclude=True)
    decision: Literal["apply", "consider", "reject", "excluded"]
    dominant_reason: str = Field(min_length=1, max_length=240)
    positives: list[str] = Field(max_length=3)
    confirmed_conflicts: list[str] = Field(max_length=3)
    unknowns: list[str] = Field(max_length=3)
    next_question: str | None = Field(default=None, max_length=240)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Golden runner input is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Golden runner input is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Golden runner input must be an object: {path}")
    return payload


def _write_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


ROLE_FAMILY_SCORES = {
    "agentic_applied_ai": 5,
    "ai_data_science": 4,
    "ai_validation_risk": 3,
    "external_deployment": 2,
    "retrieval_data": 2,
    "ai_software_fullstack": 2,
    "ml_llmops_platform": 2,
    "classical_ml": 1,
    "presales_non_build": 1,
    "model_research": 3,
}

EXCLUDED_TITLE_LEVELS = re.compile(
    r"\b(?:lead|architect|manager|head|principal|staff|junior|intern|trainee)\b",
    re.IGNORECASE,
)


def _normalize_scope(*, core_result: dict, offer: dict) -> str | None:
    """Make the title/level prefilter deterministic and keep adjacent roles scorable."""
    title = str(offer.get("title") or "")
    expected = "excluded" if EXCLUDED_TITLE_LEVELS.search(title) else "included"
    if core_result["scope_status"] == expected:
        return None
    core_result["model_scope_status"] = core_result["scope_status"]
    core_result["scope_status"] = expected
    core_result["scope_reason"] = (
        "Poziom lub tytuł jest objęty deterministycznym prefiltrem."
        if expected == "excluded"
        else "Rola sąsiednia przechodzi do pełnej oceny i ewentualnego reject."
    )
    return "Zakres został ustalony przez deterministyczny prefilter tytułu i poziomu."


def _role_direction_score(role: dict) -> int:
    score = ROLE_FAMILY_SCORES[role["core_family"]]
    if role["core_certainty"] == "ambiguous" and score == 5:
        return 4
    return score


def _normalize_role_and_fit(*, core_result: dict, offer: dict) -> str | None:
    """Apply only high-precision profile-specific rules that do not need LLM judgment."""
    requirements = " ".join(str(value) for value in offer.get("requirements") or []).casefold()
    responsibilities = " ".join(
        str(value) for value in offer.get("responsibilities") or []
    ).casefold()
    external_delivery = all(
        marker in responsibilities
        for marker in (
            "deliver ai solutions at client organizations",
            "configure integrations",
            "support adoption",
        )
    )
    if external_delivery:
        role = core_result["role_direction_fit"]
        role["model_core_family"] = role["core_family"]
        role["model_core_certainty"] = role["core_certainty"]
        role["core_family"] = "external_deployment"
        role["core_certainty"] = (
            "ambiguous" if "build custom" in responsibilities else "confirmed"
        )
        return "Dostarczanie u klientów, konfiguracja i adopcja są stałymi obowiązkami."
    fullstack_title_trap = (
        "full-stack" in requirements
        and any(marker in requirements + responsibilities for marker in ("react", "typescript"))
        and any(marker in responsibilities for marker in ("microservices", "kubernetes"))
        and "chatbot api" in responsibilities
    )
    if not fullstack_title_trap:
        return None
    role = core_result["role_direction_fit"]
    role["model_core_family"] = role["core_family"]
    role["model_core_certainty"] = role["core_certainty"]
    role["core_family"] = "ai_software_fullstack"
    role["core_certainty"] = "confirmed"
    core_result["candidate_fit"]["score"] = min(
        core_result["candidate_fit"]["score"], 2
    )
    core_result["current_cv_fit"]["score"] = min(
        core_result["current_cv_fit"]["score"], 1
    )
    core_result["tailored_cv_potential"]["score"] = min(
        core_result["tailored_cv_potential"]["score"], 1
    )
    return "Jawny wymóg full-stack i dominujące React/backend/Kubernetes z pomocniczym AI API."


def _apply_decision_policy(
    *, core_result: dict, preference_analysis: dict, offer: dict
) -> tuple[str, str]:
    if core_result["scope_status"] == "excluded":
        return "excluded", "Oferta została wykluczona przez prefilter."
    candidate_fit = core_result["candidate_fit"]["score"]
    family = core_result["role_direction_fit"]["core_family"]
    certainty = core_result["role_direction_fit"]["core_certainty"]
    if preference_analysis["hard_conflicts"]:
        return "reject", "Potwierdzony twardy konflikt warunków wymusza odrzucenie."
    if candidate_fit <= 2:
        return "reject", "Candidate Fit 1–2 wymusza odrzucenie."
    if family in {"classical_ml", "presales_non_build"}:
        return "reject", "Potwierdzony rdzeń klasycznego ML lub presales jest konfliktem kierunku."
    requirements = " ".join(str(value) for value in offer.get("requirements") or []).casefold()
    if family == "ai_software_fullstack" and "full-stack" in requirements:
        return (
            "reject",
            "Rdzeń full-stack i wymaganie tej tożsamości zawodowej wymuszają odrzucenie.",
        )
    if candidate_fit == 3:
        return "consider", "Candidate Fit 3 wymaga wyjaśnienia niepełnego opisu."
    if preference_analysis["coverage"] < 0.65:
        return "consider", "Pokrycie warunków poniżej 65% wymaga ich wyjaśnienia."
    if family == "ai_validation_risk":
        return "consider", "Rdzeń walidacji lub AI risk wymaga potwierdzenia udziału AI building."
    if family in {"external_deployment", "retrieval_data"} and certainty == "ambiguous":
        return (
            "consider",
            "Niejednoznaczny udział budowania wobec wdrożeń lub danych wymaga pytania.",
        )
    return "apply", "Brak twardych konfliktów przy Candidate Fit 4–5 pozwala aplikować."


def _prompt(case: dict, profile_context: dict, preference_analysis: dict) -> str:
    clean_case = {key: value for key, value in case.items() if key != "expected"}
    conditions = (clean_case.get("offer") or {}).get("conditions") or {}
    return (
        "Oceń dokładnie jedną ofertę. Zastosuj instrukcję systemową bez zmiany "
        "jej progów. Cytaty muszą być dosłownymi, pełnymi elementami odpowiednich "
        "list wejściowych; nie parafrazuj cytatów. Używaj wyłącznie fact_id z "
        "profile_context.career_facts. Faktów current_cv_evidence używaj tylko do "
        "Current CV Fit. Nie próbuj odgadywać oczekiwanej etykiety benchmarku. "
        "Odpowiadaj po polsku. Każde uzasadnienie ogranicz do jednego krótkiego "
        "zdania. Wybierz najwyżej dwa najmocniejsze cytaty oferty, trzy cytaty CV "
        "i cztery najmocniejsze fact_id; nie zwracaj wszystkich dostępnych dowodów. "
        "Wymaganiami są wyłącznie elementy offer.requirements: nie dopisuj wymagania "
        "na podstawie słów z responsibilities. Zawsze odczytaj offer.conditions; "
        "wartość jest unknown tylko wtedy, gdy pole jest nieobecne lub dosłownie "
        "oznaczone jako unknown. Warunki do obowiązkowego ocenienia w tym "
        "przypadku: "
        + json.dumps(conditions, ensure_ascii=False, sort_keys=True)
        + ".\n\n"
        + json.dumps(
            {
                "profile_context": profile_context,
                "deterministic_preference_analysis": preference_analysis,
                "case": clean_case,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _canonicalize_unique_quotes(quotes: list[str], allowed: set[str]) -> list[str]:
    """Expand a substantial unique source substring without weakening grounding."""
    canonical: list[str] = []
    for quote in quotes:
        if quote in allowed:
            canonical.append(quote)
            continue
        normalized = " ".join(quote.split()).casefold()
        matches = [
            candidate
            for candidate in allowed
            if len(normalized) >= 24
            and normalized in " ".join(candidate.split()).casefold()
        ]
        canonical.append(matches[0] if len(matches) == 1 else quote)
    return canonical


def _validate_semantics(
    case: dict,
    profile_context: dict,
    result: GoldenCoreEvaluation,
) -> None:
    errors: list[str] = []
    offer = case.get("offer") or {}
    allowed_offer_quotes = {
        str(value)
        for field in ("responsibilities", "requirements")
        for value in offer.get(field) or []
    }
    if offer.get("title"):
        allowed_offer_quotes.add(str(offer["title"]))
    result.role_direction_fit.offer_quotes = _canonicalize_unique_quotes(
        result.role_direction_fit.offer_quotes, allowed_offer_quotes
    )
    result.candidate_fit.offer_quotes = _canonicalize_unique_quotes(
        result.candidate_fit.offer_quotes, allowed_offer_quotes
    )
    returned_offer_quotes = (
        result.role_direction_fit.offer_quotes + result.candidate_fit.offer_quotes
    )
    invalid_quotes = sorted(set(returned_offer_quotes) - allowed_offer_quotes)
    if invalid_quotes:
        errors.append(
            "offer_quotes must be exact full input list elements: "
            + json.dumps(invalid_quotes, ensure_ascii=False)
        )

    allowed_fact_ids = {
        str(fact.get("fact_id")) for fact in profile_context.get("career_facts") or []
    }
    returned_fact_ids = (
        result.candidate_fit.profile_fact_ids + result.tailored_cv_potential.profile_fact_ids
    )
    invalid_fact_ids = sorted(set(returned_fact_ids) - allowed_fact_ids)
    if invalid_fact_ids:
        errors.append("invalid profile fact ids: " + ", ".join(invalid_fact_ids))

    allowed_cv_quotes = {str(value) for value in profile_context.get("current_cv_evidence") or []}
    if set(result.current_cv_fit.cv_quotes) - allowed_cv_quotes:
        errors.append("cv_quotes must be exact full current_cv_evidence elements")

    user_text = " ".join(
        (
            result.scope_reason,
            result.role_direction_fit.reason,
            result.candidate_fit.reason,
            result.current_cv_fit.reason,
            result.tailored_cv_potential.reason,
            result.dominant_reason,
        )
    ).casefold()
    polish_score = len(re.findall(r"[ąćęłńóśźż]", user_text)) + len(
        re.findall(r"\b(?:jest|oraz|rola|oferta|profil|pracy|brak|wymaga|zgodne)\b", user_text)
    )
    english_score = len(
        re.findall(r"\b(?:the|and|with|role|offer|candidate|experience|requirements)\b", user_text)
    )
    if polish_score < 2 or english_score > polish_score:
        errors.insert(0, "response must be in Polish")
    if errors:
        raise ValueError("; ".join(errors))


async def run_golden_v1_cases(
    *,
    client: LocalLlmClient,
    model: str,
    cases_path: Path,
    profile_context_path: Path,
    instruction_path: Path,
    output_path: Path,
    run_type: Literal["calibration", "validation"],
    max_tokens: int = 1400,
    reasoning: bool = False,
    reasoning_budget: int | None = None,
    case_ids: set[str] | None = None,
    resume: bool = False,
) -> dict:
    """Run independent, deterministic evaluations and checkpoint after every case."""
    dataset = _load_object(cases_path)
    profile_context = _load_object(profile_context_path)
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Golden runner dataset must contain a non-empty cases list")
    if case_ids:
        available = {str(case.get("case_id") or "") for case in cases}
        missing = sorted(case_ids - available)
        if missing:
            raise ValueError("Unknown Golden runner case ids: " + ", ".join(missing))
        cases = [case for case in cases if case.get("case_id") in case_ids]
    if run_type == "validation" and any("expected" in case for case in cases):
        raise ValueError("Validation inference input must not contain expected labels")

    instruction = instruction_path.read_text(encoding="utf-8")
    frozen_artifacts = {
        "cases_sha256": _sha256(cases_path),
        "profile_context_sha256": _sha256(profile_context_path),
        "instruction_sha256": _sha256(instruction_path),
    }
    started_at = datetime.now(UTC)
    retained_results: list[dict] = []
    previous_errors: list[dict] = []
    original_started_at = started_at.isoformat()
    resumed_at: list[str] = []
    if resume and output_path.is_file():
        existing = _load_object(output_path)
        if existing.get("frozen_artifacts") != frozen_artifacts:
            raise ValueError("Cannot resume: frozen Golden artifacts changed")
        if existing.get("model") != model:
            raise ValueError("Cannot resume: model changed")
        expected_runtime = {
            "seed": 42,
            "temperature": 0,
            "max_tokens": max_tokens,
            "reasoning": reasoning,
            "reasoning_budget": reasoning_budget,
        }
        if any(existing.get(key) != value for key, value in expected_runtime.items()):
            raise ValueError("Cannot resume: deterministic runtime parameters changed")
        selected_ids = {str(case.get("case_id")) for case in cases}
        retained_results = [
            result
            for result in existing.get("results") or []
            if str(result.get("case_id")) in selected_ids
        ]
        completed_ids = {str(result.get("case_id")) for result in retained_results}
        cases = [case for case in cases if str(case.get("case_id")) not in completed_ids]
        previous_errors = [
            *(existing.get("previous_errors") or []),
            *(existing.get("errors") or []),
        ]
        original_started_at = existing.get("started_at") or original_started_at
        resumed_at = [*(existing.get("resumed_at") or []), started_at.isoformat()]
    payload = {
        "schema_version": "golden-v1-model-results",
        "run_type": run_type,
        "status": "running",
        "model": model,
        "seed": 42,
        "temperature": 0,
        "max_tokens": max_tokens,
        "reasoning": reasoning,
        "reasoning_budget": reasoning_budget,
        "selected_case_ids": sorted(case_ids) if case_ids else None,
        "started_at": original_started_at,
        "resumed_at": resumed_at,
        "finished_at": None,
        "frozen_artifacts": frozen_artifacts,
        "results": retained_results,
        "errors": [],
        "previous_errors": previous_errors,
    }
    _write_checkpoint(output_path, payload)

    for case in cases:
        case_id = str(case.get("case_id") or "")
        if not case_id:
            raise ValueError("Every Golden runner case requires case_id")
        try:
            preference_analysis = calculate_preference_fit(case.get("offer") or {})
            response = await client.structured_completion(
                model=model,
                system_prompt=instruction,
                user_prompt=_prompt(case, profile_context, preference_analysis),
                schema=GoldenCoreEvaluation,
                schema_name="golden_v1_offer_evaluation",
                seed=42,
                temperature=0,
                max_tokens=max_tokens,
                strict_schema=True,
                instruction_language="pl",
                validate=lambda value: _validate_semantics(case, profile_context, value),
            )
            core_result = response.value.model_dump(mode="json")
            model_decision = core_result["decision"]
            scope_normalization_reason = _normalize_scope(
                core_result=core_result, offer=case.get("offer") or {}
            )
            normalization_reason = _normalize_role_and_fit(
                core_result=core_result, offer=case.get("offer") or {}
            )
            core_result["role_direction_fit"]["score"] = _role_direction_score(
                core_result["role_direction_fit"]
            )
            hard_conflicts = preference_analysis["hard_conflicts"]
            preference_output = {
                key: value
                for key, value in preference_analysis.items()
                if key != "hard_conflicts"
            }
            core_result["confirmed_conflicts"] = list(
                dict.fromkeys(core_result["confirmed_conflicts"] + hard_conflicts)
            )
            policy_decision, policy_reason = _apply_decision_policy(
                core_result=core_result,
                preference_analysis=preference_analysis,
                offer=case.get("offer") or {},
            )
            core_result["decision"] = policy_decision
            if policy_decision != model_decision:
                core_result["dominant_reason"] = policy_reason
            result = {
                "case_id": case_id,
                **core_result,
                "preference_fit": preference_output,
                "model_decision": model_decision,
                "decision_policy_reason": policy_reason,
                "normalization_reason": normalization_reason or scope_normalization_reason,
            }
            result["runtime"] = {
                "elapsed_ms": response.elapsed_ms,
                "schema_retries": response.retries,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            }
            payload["results"].append(result)
        except Exception as exc:  # preserve the rest of an expensive benchmark run
            payload["errors"].append(
                {"case_id": case_id, "error": f"{type(exc).__name__}: {exc}"}
            )
        _write_checkpoint(output_path, payload)

    payload["finished_at"] = datetime.now(UTC).isoformat()
    payload["status"] = "completed" if not payload["errors"] else "failed"
    _write_checkpoint(output_path, payload)
    return payload
