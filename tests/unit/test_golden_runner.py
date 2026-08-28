import json
from pathlib import Path

from job_scout.golden_runner import (
    CandidateDimension,
    CvDimension,
    GoldenCoreEvaluation,
    RoleDirectionDimension,
    TailoredCvDimension,
    _apply_decision_policy,
    _canonicalize_unique_quotes,
    _normalize_role_and_fit,
    _normalize_scope,
    _role_direction_score,
    _validate_semantics,
)

ROOT = Path(__file__).resolve().parents[2]


def _case(case_id: str) -> dict:
    payload = json.loads(
        (ROOT / "docs/golden-dataset/v1/calibration.synthetic.json").read_text(
            encoding="utf-8"
        )
    )
    return next(case for case in payload["cases"] if case["case_id"] == case_id)


def _core_result() -> dict:
    return {
        "scope_status": "included",
        "role_direction_fit": {
            "score": 5,
            "core_family": "agentic_applied_ai",
            "core_certainty": "confirmed",
        },
        "candidate_fit": {"score": 4},
        "current_cv_fit": {"score": 4},
        "tailored_cv_potential": {"score": 4},
    }


def test_external_delivery_with_custom_build_is_ambiguous_consider():
    case = _case("syn-cal-12")
    result = _core_result()
    reason = _normalize_role_and_fit(core_result=result, offer=case["offer"])
    result["role_direction_fit"]["score"] = _role_direction_score(
        result["role_direction_fit"]
    )
    decision, _ = _apply_decision_policy(
        core_result=result,
        preference_analysis={"coverage": 1.0, "hard_conflicts": []},
        offer=case["offer"],
    )

    assert reason
    assert result["role_direction_fit"]["core_family"] == "external_deployment"
    assert result["role_direction_fit"]["core_certainty"] == "ambiguous"
    assert result["role_direction_fit"]["score"] == 2
    assert decision == "consider"


def test_fullstack_ai_title_trap_is_capped_and_rejected():
    case = _case("syn-cal-14")
    result = _core_result()
    reason = _normalize_role_and_fit(core_result=result, offer=case["offer"])
    result["role_direction_fit"]["score"] = _role_direction_score(
        result["role_direction_fit"]
    )
    decision, _ = _apply_decision_policy(
        core_result=result,
        preference_analysis={"coverage": 1.0, "hard_conflicts": []},
        offer=case["offer"],
    )

    assert reason
    assert result["role_direction_fit"]["core_family"] == "ai_software_fullstack"
    assert result["candidate_fit"]["score"] == 2
    assert result["current_cv_fit"]["score"] == 1
    assert result["tailored_cv_potential"]["score"] == 1
    assert decision == "reject"


def test_unique_substantial_quote_fragment_is_expanded_to_full_source_element():
    allowed = {
        "Own batch risk scoring models and monitor drift in production",
        "Build internal reporting dashboards for the risk organization",
    }

    assert _canonicalize_unique_quotes(
        ["batch risk scoring models and monitor drift"], allowed
    ) == ["Own batch risk scoring models and monitor drift in production"]


def test_short_or_ambiguous_quote_fragment_is_not_expanded():
    allowed = {
        "Build AI assistants for internal teams",
        "Build AI assistants for external clients",
    }

    assert _canonicalize_unique_quotes(["AI assistants"], allowed) == ["AI assistants"]


def test_exact_offer_title_is_valid_role_direction_evidence():
    result = GoldenCoreEvaluation(
        scope_status="included",
        scope_reason="Oferta mieści się w zakresie.",
        role_direction_fit=RoleDirectionDimension(
            score=1,
            reason="To rola sprzedażowa, a nie budowanie systemów AI.",
            offer_quotes=["AI Solutions Consultant"],
            core_family="presales_non_build",
            core_certainty="confirmed",
        ),
        candidate_fit=CandidateDimension(
            score=1,
            reason="Brak zgodności z doświadczeniem sprzedażowym.",
            offer_quotes=["Enterprise sales experience"],
            profile_fact_ids=[],
        ),
        current_cv_fit=CvDimension(score=1, reason="Brak dowodów w CV.", cv_quotes=[]),
        tailored_cv_potential=TailoredCvDimension(
            score=1, reason="Dopasowanie wymagałoby zmiany profilu.", profile_fact_ids=[]
        ),
        decision="reject",
        dominant_reason="Rdzeniem jest presales.",
        positives=[],
        confirmed_conflicts=["Rdzeń sprzedażowy"],
        unknowns=[],
        next_question=None,
    )
    case = _case("syn-cal-15")
    profile = json.loads(
        (ROOT / "docs/golden-dataset/v1/PROFILE_CONTEXT.json").read_text(encoding="utf-8")
    )

    _validate_semantics(case, profile, result)


def test_adjacent_presales_role_is_included_for_reject_scoring():
    result = _core_result()
    result["scope_status"] = "excluded"

    reason = _normalize_scope(core_result=result, offer=_case("syn-cal-15")["offer"])

    assert reason
    assert result["scope_status"] == "included"
    assert result["model_scope_status"] == "excluded"


def test_excluded_title_level_remains_outside_scope():
    result = _core_result()
    result["scope_status"] = "included"

    reason = _normalize_scope(
        core_result=result, offer={"title": "Lead AI Agent Engineer"}
    )

    assert reason
    assert result["scope_status"] == "excluded"
