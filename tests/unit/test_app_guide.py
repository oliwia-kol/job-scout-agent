import pytest

from job_scout.app_guide import (
    AppGuideTurn,
    GuideAction,
    add_app_guide_message,
    build_app_guide_context,
    get_app_guide,
    get_or_create_app_guide,
    persist_app_guide_turn,
    validate_app_guide_turn,
)


def test_app_guide_is_available_and_persistent_without_profile(tmp_path):
    path = tmp_path / "job_scout.db"

    first = get_or_create_app_guide(path)
    second = get_or_create_app_guide(path)

    assert first["session"]["session_id"] == second["session"]["session_id"]
    assert first["messages"][0]["role"] == "assistant"
    assert "jestem Bielik" in first["messages"][0]["content"]
    assert first["messages"][0]["metadata"]["actions"][0]["path"] == "/profiles"


def test_app_guide_context_and_turn_do_not_require_profile(tmp_path):
    path = tmp_path / "job_scout.db"
    guide = get_or_create_app_guide(path)
    session_id = guide["session"]["session_id"]
    add_app_guide_message(
        path,
        session_id=session_id,
        role="user",
        content="Od czego najlepiej zacząć?",
    )
    context = build_app_guide_context(path, session_id)
    turn = AppGuideTurn(
        response_pl="Najpierw dodaj profil i CV, a aplikacja poprowadzi Cię przez OCR.",
        suggested_actions=[
            GuideAction(label="Dodaj profil i CV", path="/profiles/new")
        ],
    )

    persist_app_guide_turn(path, session_id=session_id, turn=turn)
    stored = get_app_guide(path, session_id)

    assert context["mode"] == "application_guide"
    assert context["application_state"]["ready_profile_count"] == 0
    assert [item["role"] for item in stored["messages"]] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert stored["messages"][-1]["metadata"]["actions"][0]["path"] == "/profiles/new"


def test_app_guide_rejects_invented_navigation_path(tmp_path):
    path = tmp_path / "job_scout.db"
    guide = get_or_create_app_guide(path)
    context = build_app_guide_context(path, guide["session"]["session_id"])
    turn = AppGuideTurn(
        response_pl="Możesz przejść do tej części aplikacji.",
        suggested_actions=[GuideAction(label="Nieznana funkcja", path="/invented")],
    )

    with pytest.raises(ValueError, match="unavailable or mislabeled"):
        validate_app_guide_turn(context, turn)
