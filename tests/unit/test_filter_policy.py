from job_scout.filter_policy import (
    ConditionPriority,
    ConditionState,
    VisibilityDecision,
    decide_visibility,
    user_explanation,
)


def test_unknown_never_hides_an_offer_for_any_condition_priority():
    for priority in ConditionPriority:
        assert decide_visibility(priority=priority, state=ConditionState.UNKNOWN) == (
            VisibilityDecision.SHOW
        )


def test_only_explicit_conflict_on_required_condition_hides_offer():
    assert decide_visibility(
        priority=ConditionPriority.REQUIRED,
        state=ConditionState.CONFLICT,
    ) == VisibilityDecision.HIDE
    assert decide_visibility(
        priority=ConditionPriority.IMPORTANT,
        state=ConditionState.CONFLICT,
    ) == VisibilityDecision.SHOW
    assert decide_visibility(
        priority=ConditionPriority.REQUIRED,
        state=ConditionState.MATCH,
    ) == VisibilityDecision.SHOW


def test_unknown_copy_does_not_present_missing_data_as_a_rejection():
    assert user_explanation(
        priority=ConditionPriority.REQUIRED,
        state=ConditionState.UNKNOWN,
    ) == "Nie wiadomo — oferta pozostaje widoczna"
