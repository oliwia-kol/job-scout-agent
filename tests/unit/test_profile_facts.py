from job_scout.storage import (
    list_profile_fact_conflicts,
    profile_readiness,
    resolve_profile_fact_conflict,
    review_profile_fact,
    save_profile_fact,
    save_user_profile,
)


def _profile(path):
    save_user_profile(
        path,
        profile_id="profile-facts",
        display_name="Facts",
        profile_json={"profile_id": "profile-facts", "version": 1},
        status="draft",
    )


def _add_and_approve(path, category, value, *, priority=None):
    fact_id = save_profile_fact(
        path,
        profile_id="profile-facts",
        category=category,
        value={"text": value},
        source_type="user_message",
        source_ref="message-1",
        source_quote=value,
        priority=priority,
        usable_for_scoring=True,
    )
    review_profile_fact(path, fact_id=fact_id, approve=True)
    return fact_id


def test_profile_facts_require_approval_provenance_and_all_scoring_answers(tmp_path):
    path = tmp_path / "facts.db"
    _profile(path)
    draft_id = save_profile_fact(
        path,
        profile_id="profile-facts",
        category="target_role",
        value={"text": "Applied AI Engineer"},
        source_type="user_message",
        source_ref="message-1",
        source_quote="Szukam roli Applied AI Engineer.",
        usable_for_scoring=True,
    )
    before = profile_readiness(path, "profile-facts")
    assert before["ready_for_scoring"] is False
    assert before["draft_facts"][0]["fact_id"] == draft_id

    review_profile_fact(path, fact_id=draft_id, approve=True)
    for category, value in (
        ("work_location", "Poland"),
        ("work_model", "Remote"),
        ("contract", "No constraint"),
        ("language", "English B2"),
        ("travel", "No travel"),
        ("experience", "Built a local AI prototype"),
    ):
        _add_and_approve(path, category, value)

    readiness = profile_readiness(path, "profile-facts")
    assert readiness["ready_for_scoring"] is True
    assert len(readiness["usable_for_scoring"]) == 7
    assert all(item["source_quote"] for item in readiness["approved_facts"])
    assert max(item["profile_version"] for item in readiness["approved_facts"]) == 8


def test_conflicting_approved_single_value_facts_block_scoring(tmp_path):
    path = tmp_path / "facts.db"
    _profile(path)
    _add_and_approve(path, "work_model", "Remote")
    _add_and_approve(path, "work_model", "Office only")

    conflicts = list_profile_fact_conflicts(path, "profile-facts")
    readiness = profile_readiness(path, "profile-facts")
    assert len(conflicts) == 1
    assert conflicts[0]["status"] == "open"
    assert readiness["ready_for_scoring"] is False
    assert readiness["conflicts"][0]["category"] == "work_model"


def test_only_scoring_usable_facts_unlock_gate_and_conflict_supersedes_loser(tmp_path):
    path = tmp_path / "facts.db"
    _profile(path)
    for category, value in (
        ("target_role", "Applied AI Engineer"),
        ("work_location", "Poland"),
        ("contract", "No constraint"),
        ("language", "English B2"),
        ("travel", "No travel"),
        ("experience", "Built a local AI prototype"),
    ):
        _add_and_approve(path, category, value)
    disabled_id = save_profile_fact(
        path,
        profile_id="profile-facts",
        category="work_model",
        value={"text": "Office only"},
        source_type="user_message",
        source_ref="message-1",
        source_quote="Office only",
        usable_for_scoring=False,
    )
    review_profile_fact(path, fact_id=disabled_id, approve=True)
    assert profile_readiness(path, "profile-facts")["ready_for_scoring"] is False

    active_id = _add_and_approve(path, "work_model", "Remote")
    conflicts = list_profile_fact_conflicts(path, "profile-facts")
    assert len(conflicts) == 1
    resolve_profile_fact_conflict(
        path,
        conflict_id=conflicts[0]["conflict_id"],
        winner_fact_id=active_id,
        resolution_note="Remote is the explicit current preference.",
    )
    readiness = profile_readiness(path, "profile-facts")
    assert readiness["ready_for_scoring"] is True
    assert active_id in {fact["fact_id"] for fact in readiness["usable_for_scoring"]}
    assert disabled_id not in {fact["fact_id"] for fact in readiness["approved_facts"]}
