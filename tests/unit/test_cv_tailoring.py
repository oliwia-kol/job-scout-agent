import hashlib
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from job_scout.cv_tailoring import (
    CvSuggestionBatch,
    CvTailoringError,
    QwenCvTailoringResponder,
    SuggestedCvChange,
    approve_master_mapping,
    build_application_package,
    build_tailoring_context,
    compose_tailored_html,
    get_application_package,
    get_cv_document,
    import_master_cv,
    prepare_print_html,
    record_package_event,
    replace_master_cv,
    review_suggestion,
    sanitize_inline_html,
    sanitize_master_html,
    validate_suggestion,
)
from job_scout.storage import connect, initialize_database

MASTER = b"""<!doctype html><html><head>
<style>.profile-text{color:#222} .x{background:url(https://bad.example/x)}</style>
<script>alert(1)</script></head><body onload="bad()">
<div class="highlights-bar"><div class="hl-value">Built a useful automation.</div></div>
<div class="left-col"><section class="section"><h2 class="section-title">Experience</h2>
<div class="item-title">Engineer</div><div class="item-desc">Built Python tools.</div>
</section></div>
<section><h2 class="section-title">Profile</h2>
<div class="profile-text">AI engineer.</div></section>
<section><h2 class="section-title">Skills</h2><div class="skill-items">Python</div></section>
<section><h2 class="section-title">Certifications</h2>
<div class="cert-item"><div class="cert-name">TensorFlow Developer</div>
<div class="cert-org">DeepLearning.AI</div></div>
<div class="cert-item"><div class="cert-name">AI Agents</div>
<div class="cert-org">Vanderbilt University</div></div></section>
<div class="gdpr">I agree to the processing of my personal data under GDPR.</div>
<iframe src="https://bad.example"></iframe></body></html>"""


def _database(path: Path) -> None:
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO user_profiles (
            profile_id, display_name, profile_json, status, current_version,
            completeness_status, created_at, updated_at)
            VALUES ('profile', 'Profile', '{}', 'ready', 1, 'ready_for_scoring', ?, ?)""",
            ("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00"),
        )


def test_master_sanitizer_removes_active_content_and_maps_only_editable_blocks():
    safe, sections = sanitize_master_html(MASTER)

    assert "<script" not in safe
    assert "<iframe" not in safe
    assert "onload" not in safe
    assert "https://bad.example" not in safe
    assert {item["section_kind"] for item in sections} == {
        "highlights",
        "experience",
        "profile",
        "skills",
        "certifications",
    }
    assert "data-cv-anchor" in safe
    assert "Engineer" not in {item["current_text"] for item in sections}
    assert "GDPR" not in {item["current_text"] for item in sections}


def test_master_import_is_immutable_and_requires_mapping_approval(tmp_path):
    database = tmp_path / "db.sqlite"
    _database(database)

    document_id = import_master_cv(
        database,
        storage_root=tmp_path / "documents",
        profile_id="profile",
        filename="master.html",
        content=MASTER,
    )
    document = get_cv_document(database, document_id)

    assert document is not None
    assert Path(document["stored_path"]).read_bytes() == MASTER
    assert Path(document["safe_path"]).read_bytes() != MASTER
    assert document["status"] == "draft"
    approve_master_mapping(database, document_id)
    assert get_cv_document(database, document_id)["status"] == "mapped"
    with pytest.raises(CvTailoringError, match="active master"):
        import_master_cv(
            database,
            storage_root=tmp_path / "documents",
            profile_id="profile",
            filename="second.html",
            content=MASTER,
        )


def test_master_replacement_preserves_previous_version(tmp_path):
    database = tmp_path / "db.sqlite"
    _database(database)
    storage_root = tmp_path / "documents"
    previous_id = import_master_cv(
        database,
        storage_root=storage_root,
        profile_id="profile",
        filename="master.html",
        content=MASTER,
    )
    approve_master_mapping(database, previous_id)
    replacement = MASTER.replace(b"Built Python tools.", b"Built reliable Python tools.")

    current_id = replace_master_cv(
        database,
        storage_root=storage_root,
        profile_id="profile",
        filename="master-v2.html",
        content=replacement,
    )

    previous = get_cv_document(database, previous_id)
    current = get_cv_document(database, current_id)
    assert previous["status"] == "superseded"
    assert Path(previous["stored_path"]).read_bytes() == MASTER
    assert current["status"] == "draft"
    assert current["parent_document_id"] == previous_id
    assert Path(current["stored_path"]).read_bytes() == replacement


@pytest.mark.asyncio
async def test_tailoring_prompt_uses_calibrated_ownership_narrative():
    class CapturingClient:
        request = None

        async def structured_completion(self, **kwargs):
            self.request = kwargs
            return type(
                "Response",
                (),
                {"value": CvSuggestionBatch(suggestions=[])},
            )()

    client = CapturingClient()
    responder = QwenCvTailoringResponder(client, "test-model")

    await responder.respond({"editable_sections": []})

    prompt = client.request["system_prompt"]
    assert "confident but calibrated ownership" in prompt
    assert "Do not add an AI-assistance disclaimer" in prompt
    assert "technology used by the system" in prompt
    assert "do not imply production deployment" in prompt
    assert "physics-trained Applied AI professional" in prompt
    assert "curiosity evidence-based" in prompt
    assert "LLM APIs and local inference" in prompt
    assert "not the target identity" in prompt


def test_inline_html_allows_only_strong_and_em():
    assert sanitize_inline_html(
        '<strong class="x">Built</strong> <a href="https://bad">AI</a> <em>tools</em>'
    ) == "<strong>Built</strong> AI <em>tools</em>"


def test_suggestion_contract_rejects_protected_target_and_invented_metric():
    context = {
        "editable_sections": [{
            "section_id": "section-1",
            "anchor_sha256": "a" * 64,
            "current_text": "Built Python tools.",
        }],
        "profile_evidence": [{"fact_id": "fact-1", "value": "Python"}],
        "career_knowledge": [],
        "offer_evidence": [{"evidence_id": "offer-1", "value": "Python"}],
    }
    base = {
        "section_id": "section-1",
        "anchor_sha256": "a" * 64,
        "current_text": "Built Python tools.",
        "intent": "Emphasize relevant tooling",
        "profile_evidence_ids": ["fact-1"],
        "offer_evidence_ids": ["offer-1"],
    }

    validate_suggestion(
        context,
        SuggestedCvChange(**base, proposed_html="Built <strong>Python</strong> tools."),
    )
    with pytest.raises(CvTailoringError, match="unknown or protected"):
        validate_suggestion(
            context,
            SuggestedCvChange(**{**base, "section_id": "job-title"}, proposed_html="Lead"),
        )
    with pytest.raises(CvTailoringError, match="unsupported numbers"):
        validate_suggestion(
            context,
            SuggestedCvChange(**base, proposed_html="Improved delivery by 40%."),
        )


def test_certification_suggestions_can_select_and_reorder_only_existing_pool():
    current_html = (
        '<div class="cert-item"><div class="cert-name">TensorFlow Developer</div>'
        '<div class="cert-org">DeepLearning.AI</div></div>'
        '<div class="cert-item"><div class="cert-name">AI Agents</div>'
        '<div class="cert-org">Vanderbilt University</div></div>'
    )
    context = {
        "editable_sections": [{
            "section_id": "cert-1",
            "section_kind": "certifications",
            "anchor_sha256": "b" * 64,
            "current_html": current_html,
            "current_text": "TensorFlow Developer DeepLearning.AI | AI Agents Vanderbilt",
        }],
        "profile_evidence": [],
        "career_knowledge": [],
        "offer_evidence": [{"evidence_id": "offer-1", "value": "LLM agents"}],
    }
    base = {
        "section_id": "cert-1",
        "anchor_sha256": "b" * 64,
        "current_text": "TensorFlow Developer DeepLearning.AI | AI Agents Vanderbilt",
        "intent": "Keep only certifications relevant to the role",
        "profile_evidence_ids": [],
        "offer_evidence_ids": ["offer-1"],
    }

    validate_suggestion(
        context,
        SuggestedCvChange(**base, proposed_html=current_html),
    )
    selected = (
        '<div class="cert-item"><div class="cert-name">AI Agents</div>'
        '<div class="cert-org">Vanderbilt University</div></div>'
    )
    validate_suggestion(context, SuggestedCvChange(**base, proposed_html=selected))
    added_from_cv_pool = (
        '<div class="cert-item"><div class="cert-name">LangGraph Agents</div>'
        '<div class="cert-org">DeepLearning.AI</div></div>'
    )
    validate_suggestion(
        {
            **context,
            "cv_assets": [{
                "category": "certification",
                "value": {"name": "LangGraph Agents", "issuer": "DeepLearning.AI"},
            }],
        },
        SuggestedCvChange(**base, proposed_html=added_from_cv_pool),
    )
    with pytest.raises(CvTailoringError, match="existing pool"):
        validate_suggestion(
            context,
            SuggestedCvChange(
                **base,
                proposed_html=(
                    '<div class="cert-item"><div class="cert-name">New Cert</div>'
                    '<div class="cert-org">Unknown</div></div>'
                ),
            ),
        )


def test_skills_suggestion_cannot_grow_too_much_for_layout():
    context = {
        "editable_sections": [{
            "section_id": "skills-1",
            "section_kind": "skills",
            "anchor_sha256": "c" * 64,
            "current_text": "Python, SQL, LLMs",
        }],
        "profile_evidence": [{"fact_id": "fact-1", "value": "Python"}],
        "career_knowledge": [],
        "offer_evidence": [{"evidence_id": "offer-1", "value": "Python"}],
    }
    base = {
        "section_id": "skills-1",
        "anchor_sha256": "c" * 64,
        "current_text": "Python, SQL, LLMs",
        "intent": "Add keywords",
        "profile_evidence_ids": ["fact-1"],
        "offer_evidence_ids": ["offer-1"],
    }

    validate_suggestion(
        context,
        SuggestedCvChange(**base, proposed_html="Python, SQL, LLMs, RAG"),
    )
    with pytest.raises(CvTailoringError, match="too long"):
        validate_suggestion(
            context,
            SuggestedCvChange(
                **base,
                proposed_html=(
                    "Python, SQL, LLMs, RAG, agents, MCP, LangGraph, FastAPI, Docker, "
                    "Azure, GCP, evaluation, observability, prompt engineering, vector DBs"
                ),
            ),
        )


def test_accepted_edited_suggestion_is_composed_without_touching_title(tmp_path):
    database = tmp_path / "db.sqlite"
    _database(database)
    document_id = import_master_cv(
        database,
        storage_root=tmp_path / "documents",
        profile_id="profile",
        filename="master.html",
        content=MASTER,
    )
    approve_master_mapping(database, document_id)
    document = get_cv_document(database, document_id)
    section = next(item for item in document["sections"] if item["section_kind"] == "experience")
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO offers (
            source_id, identity_key, company, title, job_url, offer_json,
            application_status, first_seen_at, last_seen_at)
            VALUES ('test', 'test:1', 'Example', 'AI Engineer', 'https://example.test/1',
            '{}', 'applying', ?, ?)""",
            ("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00"),
        )
        offer_id = connection.execute("SELECT id FROM offers").fetchone()[0]
        connection.execute(
            """INSERT INTO cv_tailoring_sessions (
            session_id, profile_id, profile_version, offer_id, offer_content_sha256,
            document_id, prompt_version, status, created_at, updated_at)
            VALUES ('session', 'profile', 1, ?, ?, ?, 'test', 'review', ?, ?)""",
            (
                offer_id,
                hashlib.sha256(b"{}").hexdigest(),
                document_id,
                "2026-08-11T00:00:00+00:00",
                "2026-08-11T00:00:00+00:00",
            ),
        )
        connection.execute(
            """INSERT INTO cv_suggestions (
            suggestion_id, session_id, section_id, anchor_sha256, current_html,
            current_text, proposed_html, intent, status, created_at)
            VALUES ('suggestion', 'session', ?, ?, ?, ?, '<strong>Built</strong> tools.',
            'Match role', 'proposed', ?)""",
            (
                section["section_id"],
                section["content_sha256"],
                section["current_html"],
                section["current_text"],
                "2026-08-11T00:00:00+00:00",
            ),
        )

    review_suggestion(
        database,
        suggestion_id="suggestion",
        decision="accepted",
        edited_html="Built <em>production</em> tools.",
    )
    output = compose_tailored_html(database, "session")
    soup = BeautifulSoup(output, "html.parser")

    assert soup.select_one(".item-title").get_text(strip=True) == "Engineer"
    assert soup.select_one(".item-desc").get_text(" ", strip=True) == "Built production tools."
    assert soup.select_one(".item-desc em").get_text(strip=True) == "production"
    assert soup.select_one(".gdpr").get_text(" ", strip=True) == (
        "I agree to the processing of my personal data under GDPR."
    )


def test_accepted_certification_selection_does_not_touch_gdpr_footer(tmp_path):
    database = tmp_path / "db.sqlite"
    _database(database)
    document_id = import_master_cv(
        database,
        storage_root=tmp_path / "documents",
        profile_id="profile",
        filename="master.html",
        content=MASTER,
    )
    approve_master_mapping(database, document_id)
    document = get_cv_document(database, document_id)
    section = next(
        item for item in document["sections"] if item["section_kind"] == "certifications"
    )
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO offers (
            source_id, identity_key, company, title, job_url, offer_json,
            application_status, first_seen_at, last_seen_at)
            VALUES ('test', 'test:cert', 'Example', 'AI Engineer', 'https://example.test/cert',
            '{}', 'applying', ?, ?)""",
            ("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00"),
        )
        offer_id = connection.execute("SELECT id FROM offers").fetchone()[0]
        connection.execute(
            """INSERT INTO cv_tailoring_sessions (
            session_id, profile_id, profile_version, offer_id, offer_content_sha256,
            document_id, prompt_version, status, created_at, updated_at)
            VALUES ('cert-session', 'profile', 1, ?, ?, ?, 'test', 'review', ?, ?)""",
            (
                offer_id,
                hashlib.sha256(b"{}").hexdigest(),
                document_id,
                "2026-08-11T00:00:00+00:00",
                "2026-08-11T00:00:00+00:00",
            ),
        )
        connection.execute(
            """INSERT INTO cv_suggestions (
            suggestion_id, session_id, section_id, anchor_sha256, current_html,
            current_text, proposed_html, intent, status, created_at)
            VALUES ('cert-suggestion', 'cert-session', ?, ?, ?, ?, ?,
            'Remove less relevant certification', 'proposed', ?)""",
            (
                section["section_id"],
                section["content_sha256"],
                section["current_html"],
                section["current_text"],
                (
                    '<div class="cert-item"><div class="cert-name">AI Agents</div>'
                    '<div class="cert-org">Vanderbilt University</div></div>'
                ),
                "2026-08-11T00:00:00+00:00",
            ),
        )

    review_suggestion(database, suggestion_id="cert-suggestion", decision="accepted")
    output = compose_tailored_html(database, "cert-session")
    soup = BeautifulSoup(output, "html.parser")

    assert "TensorFlow Developer" not in soup.get_text(" ", strip=True)
    assert "AI Agents" in soup.get_text(" ", strip=True)
    assert soup.select_one(".gdpr").get_text(" ", strip=True) == (
        "I agree to the processing of my personal data under GDPR."
    )


def test_master_rejects_non_utf8_and_missing_required_sections():
    with pytest.raises(CvTailoringError, match="UTF-8"):
        sanitize_master_html(b"\xff\xfe")
    with pytest.raises(CvTailoringError, match="required editable sections"):
        sanitize_master_html(b"<html><body><section><h2>Profile</h2></section></body></html>")


def test_print_contract_forces_a4_and_removes_forced_page_breaks():
    output = prepare_print_html(MASTER.decode())

    assert "@page { size: A4" in output
    assert "#education-section, #research-item" in output
    assert 'data-cv-print-contract="a4-v1"' in output


def test_changed_profile_marks_tailoring_session_stale(tmp_path):
    database = tmp_path / "db.sqlite"
    _database(database)
    document_id = import_master_cv(
        database,
        storage_root=tmp_path / "documents",
        profile_id="profile",
        filename="master.html",
        content=MASTER,
    )
    approve_master_mapping(database, document_id)
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO offers (
            source_id, identity_key, company, title, job_url, offer_json,
            first_seen_at, last_seen_at)
            VALUES ('test', 'test:stale', 'Example', 'AI Engineer',
            'https://example.test/stale', '{}', ?, ?)""",
            ("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00"),
        )
        offer_id = connection.execute(
            "SELECT id FROM offers WHERE identity_key = 'test:stale'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO cv_tailoring_sessions (
            session_id, profile_id, profile_version, offer_id, offer_content_sha256,
            document_id, prompt_version, status, created_at, updated_at)
            VALUES ('stale-session', 'profile', 1, ?, ?, ?, 'test', 'ready', ?, ?)""",
            (
                offer_id,
                hashlib.sha256(b"{}").hexdigest(),
                document_id,
                "2026-08-11T00:00:00+00:00",
                "2026-08-11T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE user_profiles SET current_version = 2 WHERE profile_id = 'profile'"
        )

    with pytest.raises(CvTailoringError, match="profile changed"):
        build_tailoring_context(database, "stale-session")
    with connect(database) as connection:
        row = connection.execute(
            "SELECT status, error FROM cv_tailoring_sessions WHERE session_id = 'stale-session'"
        ).fetchone()
    assert tuple(row) == ("stale", "profile changed")


@pytest.mark.asyncio
async def test_package_persists_hashes_and_does_not_mark_offer_applied(tmp_path, monkeypatch):
    database = tmp_path / "db.sqlite"
    _database(database)
    document_id = import_master_cv(
        database,
        storage_root=tmp_path / "documents",
        profile_id="profile",
        filename="master.html",
        content=MASTER,
    )
    approve_master_mapping(database, document_id)
    with connect(database) as connection:
        connection.execute(
            """INSERT INTO offers (
            source_id, identity_key, company, title, job_url, offer_json,
            application_status, first_seen_at, last_seen_at)
            VALUES ('test', 'test:package', 'Example', 'AI Engineer',
            'https://example.test/package', '{}', 'applying', ?, ?)""",
            ("2026-08-11T00:00:00+00:00", "2026-08-11T00:00:00+00:00"),
        )
        offer_id = connection.execute(
            "SELECT id FROM offers WHERE identity_key = 'test:package'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO cv_tailoring_sessions (
            session_id, profile_id, profile_version, offer_id, offer_content_sha256,
            document_id, prompt_version, status, created_at, updated_at)
            VALUES ('package-session', 'profile', 1, ?, ?, ?, 'test', 'ready', ?, ?)""",
            (
                offer_id,
                hashlib.sha256(b"{}").hexdigest(),
                document_id,
                "2026-08-11T00:00:00+00:00",
                "2026-08-11T00:00:00+00:00",
            ),
        )

    async def renderer(_html, output_path):
        output_path.write_bytes(b"%PDF" + b"x" * 12_000)

    monkeypatch.setattr(
        "job_scout.cv_tailoring.validate_pdf_artifact",
        lambda _path: {
            "page_count": 2,
            "page_sizes": [(595.3, 841.9), (595.3, 841.9)],
            "selectable_characters": 2000,
            "a4": True,
        },
    )
    package_id = await build_application_package(
        database,
        session_id="package-session",
        storage_root=tmp_path / "packages",
        renderer=renderer,
    )
    package = get_application_package(database, package_id)

    assert package is not None
    assert Path(package["pdf_path"]).is_file()
    assert len(package["pdf_sha256"]) == 64
    with connect(database) as connection:
        assert connection.execute(
            "SELECT application_status FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()[0] == "applying"
    record_package_event(database, package_id=package_id, event_type="applied")
    with connect(database) as connection:
        assert connection.execute(
            "SELECT application_status FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()[0] == "applied"
