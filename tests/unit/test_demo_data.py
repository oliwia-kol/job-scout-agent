from pathlib import Path

from job_scout.demo_data import (
    build_model_input_snapshot,
    load_candidate_profile,
    load_frozen_demo_offers,
    normalize_model_text,
)

ROOT = Path(__file__).parents[2]


def test_candidate_profile_is_versioned_and_approved():
    profile = load_candidate_profile(ROOT / "config/demo/candidate-profile-v2.json")
    assert profile.profile_id == "synthetic-candidate-v2"
    assert profile.version == 2
    assert profile.approved is True
    assert len(profile.evidence) == 5


def test_frozen_demo_contains_exactly_the_approved_ten_offers():
    offers = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)
    assert len(offers) == 10
    assert len({(offer.company, offer.title) for offer in offers}) == 10
    assert offers[0].company == "Addepto"
    assert offers[-1].company == "Xebia"


def test_model_text_removes_encoded_and_literal_html():
    assert normalize_model_text("Build &lt;li&gt;safe&lt;/li&gt; <b>AI</b>") == "Build safe AI"


def test_model_input_snapshot_keeps_scoring_and_audit_fields():
    offer = load_frozen_demo_offers(ROOT / "config/demo/frozen-offers-v1.json", ROOT)[0]
    snapshot = build_model_input_snapshot(offer)
    assert snapshot["company"] == "Addepto"
    assert snapshot["analysis_text"]
    assert len(snapshot["raw_sha256"]) == 64
    assert "supplemental_info" in snapshot
