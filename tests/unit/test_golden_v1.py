import json
from pathlib import Path

import pytest

from job_scout.golden_v1 import score_golden_v1_results, validate_golden_v1

ROOT = Path(__file__).resolve().parents[2]
V1 = ROOT / "docs/golden-dataset/v1"
PRIVATE_KEY = ROOT / "data/golden-private/v1/validation.answer-key.json"

pytestmark = pytest.mark.skipif(
    not PRIVATE_KEY.exists(),
    reason="private Golden v1 holdout key is intentionally not published",
)


def _validate(**overrides):
    paths = {
        "calibration_path": V1 / "calibration.synthetic.json",
        "validation_inputs_path": V1 / "validation.inputs.synthetic.json",
        "validation_key_path": PRIVATE_KEY,
        "prefilter_path": V1 / "prefilter.synthetic.json",
        "instruction_path": V1 / "MODEL_EVALUATION_INSTRUCTION.md",
        "profile_context_path": V1 / "PROFILE_CONTEXT.json",
    }
    paths.update(overrides)
    return validate_golden_v1(**paths)


def test_golden_v1_is_balanced_diverse_and_holdout_isolated():
    report = _validate()
    assert report["calibration_labels"] == {"apply": 6, "consider": 6, "reject": 6}
    assert report["validation_labels"] == {"apply": 6, "consider": 6, "reject": 6}
    assert report["unique_tags"] >= 24
    assert len(report["validation_inputs_sha256"]) == 64
    assert len(report["validation_key_sha256"]) == 64
    assert len(report["profile_context_sha256"]) == 64


def test_golden_v1_rejects_label_leakage_in_public_validation_inputs(tmp_path):
    payload = json.loads((V1 / "validation.inputs.synthetic.json").read_text(encoding="utf-8"))
    payload["cases"][0]["expected"] = {"decision": "apply"}
    contaminated = tmp_path / "validation.json"
    contaminated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="leak labels"):
        _validate(validation_inputs_path=contaminated)


def test_golden_v1_scorer_reports_perfect_grounded_holdout_without_inference_leak(tmp_path):
    inputs = json.loads((V1 / "validation.inputs.synthetic.json").read_text(encoding="utf-8"))
    key = json.loads(PRIVATE_KEY.read_text(encoding="utf-8"))
    input_by_id = {case["case_id"]: case for case in inputs["cases"]}
    results = []
    for expected in key["cases"]:
        quote = input_by_id[expected["case_id"]]["offer"]["responsibilities"][0]
        results.append(
            {
                "case_id": expected["case_id"],
                "decision": expected["decision"],
                "dominant_reason": expected["dominant_reason"],
                "role_direction_fit": {
                    "score": expected["role_direction_fit"],
                    "offer_quotes": [quote],
                },
                "candidate_fit": {
                    "score": expected["candidate_fit"],
                    "offer_quotes": [quote],
                },
                "current_cv_fit": {"score": expected["current_cv_fit"]},
                "tailored_cv_potential": {"score": expected["tailored_cv_potential"]},
            }
        )
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({"results": results}), encoding="utf-8")

    report = score_golden_v1_results(
        results_path=results_path,
        validation_inputs_path=V1 / "validation.inputs.synthetic.json",
        validation_key_path=PRIVATE_KEY,
    )

    assert report["decision_accuracy"] == 1
    assert report["macro_f1"] == 1
    assert report["grounded_evidence_rate"] == 1
    assert report["passed_working_thresholds"] is True
    assert report["mismatches"] == []
