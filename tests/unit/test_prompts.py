from job_scout.prompts import EVALUATOR_INSTRUCTION, EVALUATOR_PROMPT_VERSION


def test_evaluator_prompt_has_unambiguous_scales_and_evidence_requirement():
    assert EVALUATOR_PROMPT_VERSION == "evaluator-v5-role-direction"
    assert "0.0–10.0" in EVALUATOR_INSTRUCTION
    assert "0–100" in EVALUATOR_INSTRUCTION
    assert "0.0–1.0" in EVALUATOR_INSTRUCTION
    assert "evidence MUST contain at least one item" in EVALUATOR_INSTRUCTION
    assert "means unknown" in EVALUATOR_INSTRUCTION
    assert "never a\n  conflict" in EVALUATOR_INSTRUCTION
    assert "English B2" in EVALUATOR_INSTRUCTION
    assert "dominant daily work" in EVALUATOR_INSTRUCTION
    assert "role_direction_preferences" in EVALUATOR_INSTRUCTION
