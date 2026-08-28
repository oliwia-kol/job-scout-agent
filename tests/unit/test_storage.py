import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_scout.ats_scrapers import CleanJob
from job_scout.domain import (
    CandidateEvidence,
    CandidateProfile,
    EvaluationItemStatus,
    EvaluationRunItem,
    PipelineRun,
)
from job_scout.language import CanonicalEnglishTranslation, TranslationQuoteMap
from job_scout.storage import (
    approve_offer_translation,
    approve_profile_document,
    backfill_offer_languages,
    backfill_offer_ledger,
    cancel_run,
    connect,
    create_evaluation_run,
    enqueue_export,
    get_latest_offer_translation,
    get_offer,
    get_pipeline_run,
    get_user_profile,
    initialize_database,
    list_evaluation_run_items,
    list_lab_experiments,
    list_lab_model_profiles,
    list_notifications,
    list_offer_events,
    list_offer_versions,
    list_pending_exports,
    list_profile_versions,
    mark_interrupted_runs,
    mark_notification_read,
    persist_cancelled_collection,
    persist_clean_offer,
    persist_monitored_collection,
    queue_notification_delivery,
    record_export_result,
    restore_backup_drill,
    resume_run,
    save_lab_experiment,
    save_lab_model_profile,
    save_offer_translation,
    save_profile_document,
    save_user_profile,
    save_web_push_subscription,
    update_evaluation_run_item,
)


def clean_offer(**overrides):
    values = {
        "source_id": "example",
        "company": "Example AI",
        "external_id": "job-42",
        "title": "AI Engineer",
        "url": "https://example.com/jobs/42",
        "locations": ["Warsaw, Poland"],
        "description": "Build AI products in Poland.",
        "analysis_text": "Build AI products in Poland.",
        "raw_sha256": "a" * 64,
        "raw_payload": "<html>first version</html>",
        "extraction_method": "html",
    }
    values.update(overrides)
    return CleanJob(**values)


def candidate_profile(*, approved=True):
    return CandidateProfile(
        profile_id="synthetic-candidate-v2",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[CandidateEvidence(id="python", statement="Uses Python", source="cv")],
        location_rule="Remote from Poland or Warsaw",
        approved_at=datetime.now(UTC) if approved else None,
    )


def _create_legacy_database(path, *, partially_migrated=False):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE raw_offers (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, company TEXT NOT NULL,
                job_url TEXT NOT NULL, external_id TEXT, payload TEXT NOT NULL,
                content_type TEXT NOT NULL, fetched_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            );
            CREATE TABLE offers (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, company TEXT NOT NULL,
                title TEXT NOT NULL, job_url TEXT NOT NULL UNIQUE, offer_json TEXT NOT NULL,
                assessment_json TEXT, judge_json TEXT,
                application_status TEXT NOT NULL DEFAULT 'new',
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE pipeline_runs (
                run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL, models_json TEXT NOT NULL, error TEXT
            );
            """
        )
        if partially_migrated:
            connection.execute(
                "ALTER TABLE raw_offers ADD COLUMN source_url TEXT NOT NULL DEFAULT ''"
            )
            connection.execute("ALTER TABLE raw_offers ADD COLUMN identity_key TEXT")
            connection.execute("ALTER TABLE offers ADD COLUMN identity_key TEXT")
            connection.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN run_type TEXT NOT NULL DEFAULT 'collection'"
            )
        connection.execute(
            """
            INSERT INTO raw_offers (
                source_id, company, job_url, external_id, payload, content_type,
                fetched_at, payload_sha256
            ) VALUES ('legacy', 'Legacy AI', 'https://example.com/legacy', 'legacy-1',
                      '{}', 'json', '2026-07-01T00:00:00+00:00', ?)
            """,
            ("a" * 64,),
        )
        connection.execute(
            """
            INSERT INTO offers (
                source_id, company, title, job_url, offer_json, first_seen_at, last_seen_at
            ) VALUES ('legacy', 'Legacy AI', 'Legacy Engineer',
                      'https://example.com/legacy', '{}',
                      '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, source_id, started_at, status, models_json
            ) VALUES ('legacy-run', 'legacy', '2026-07-01T00:00:00+00:00',
                      'completed', '{}')
            """
        )


def _create_run_with_items(path, run_id, run_status, item_statuses):
    items = []
    for index, status in enumerate(item_statuses, start=1):
        offer = clean_offer(
            external_id=f"job-{run_id}-{index}",
            title=f"Offer {index}",
            url=f"https://example.com/jobs/{run_id}/{index}",
            raw_sha256=f"{index:x}" * 64,
        )
        with connect(path) as connection:
            offer_id, _ = persist_clean_offer(
                connection, offer, source_url="https://example.com/careers"
            )
        items.append(
            EvaluationRunItem(
                item_id=f"{run_id}:{offer_id}",
                run_id=run_id,
                offer_id=offer_id,
                status=status,
                input_sha256=f"{index + 5:x}" * 64,
                input_snapshot={"offer": index},
            )
        )
    run = PipelineRun(
        run_id=run_id,
        source_id="demo",
        status=run_status,
        total_items=len(items),
        completed_items=sum(
            status in {EvaluationItemStatus.COMPLETED, EvaluationItemStatus.FAILED}
            for status in item_statuses
        ),
        current_stage=run_status.value,
        finished_at=(
            datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
            if run_status != "running"
            else None
        ),
        profile_id="synthetic-candidate-v2",
        profile_version=1,
        error="old run error" if run_status != "running" else None,
    )
    create_evaluation_run(path, run, candidate_profile(), items)
    return items


def test_initialize_database_creates_versioned_schema(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version")]
    assert {
        "raw_offers",
        "offers",
        "pipeline_runs",
        "evaluation_run_items",
        "user_feedback",
    } <= tables
    assert versions == list(range(1, 18))
    assert "profile_versions" in tables
    assert {
        "cv_documents",
        "cv_sections",
        "cv_tailoring_sessions",
        "cv_suggestions",
        "application_packages",
        "application_events",
    } <= tables


def test_cancelled_collection_is_kept_without_partial_offer_data(tmp_path):
    path = tmp_path / "job_scout.db"
    persist_cancelled_collection(
        path,
        run_id="cancelled-scan",
        mode="quick",
        started_at="2026-07-30T10:00:00+00:00",
        sources_total=3,
        sources_checked=1,
        current_source="Example AI",
    )
    with connect(path) as connection:
        run = connection.execute(
            "SELECT status, offers_saved, error FROM collection_runs WHERE run_id='cancelled-scan'"
        ).fetchone()
    assert dict(run) == {
        "status": "cancelled",
        "offers_saved": 0,
        "error": "cancelled while checking Example AI",
    }


def test_migrates_legacy_database_and_preserves_data(tmp_path):
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)

    initialize_database(path)

    with connect(path) as connection:
        raw = connection.execute(
            "SELECT identity_key, source_url FROM raw_offers"
        ).fetchone()
        offer = connection.execute("SELECT identity_key, title FROM offers").fetchone()
        run = connection.execute(
            "SELECT run_type, status, models_json FROM pipeline_runs"
        ).fetchone()
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_version")]
    assert raw["identity_key"] == "legacy:legacy-1"
    assert raw["source_url"] == ""
    assert offer["identity_key"] == "legacy:https://example.com/legacy"
    assert offer["title"] == "Legacy Engineer"
    assert dict(run) == {"run_type": "collection", "status": "completed", "models_json": "{}"}
    assert versions == list(range(1, 18))


def test_migrates_partially_upgraded_database_without_duplicate_columns(tmp_path):
    path = tmp_path / "partial.db"
    _create_legacy_database(path, partially_migrated=True)

    initialize_database(path)

    with connect(path) as connection:
        assert "current_stage" in {
            row["name"] for row in connection.execute("PRAGMA table_info(pipeline_runs)")
        }
        assert connection.execute("SELECT count(*) FROM offers").fetchone()[0] == 1


def test_v5_profile_representations_migrate_without_data_loss(tmp_path):
    path = tmp_path / "v5-profiles.db"
    migrations = Path(__file__).parents[2] / "src/job_scout/migrations"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"
        )
        for version, filename in (
            (1, "001_initial.sql"),
            (2, "002_add_profiles_and_diagnostics.sql"),
            (3, "003_add_run_parent.sql"),
            (4, "004_add_collection_monitoring.sql"),
            (5, "005_add_profiles_and_llm_lab.sql"),
        ):
            connection.executescript((migrations / filename).read_text())
            connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        candidate_json = json.dumps(
            {
                "profile_id": "legacy-candidate",
                "version": 2,
                "target_roles": ["AI Engineer"],
                "evidence": [{"id": "proof", "statement": "Evidence", "source": "CV"}],
                "location_rule": "Remote",
                "approved_at": "2026-07-01T00:00:00+00:00",
            }
        )
        connection.execute(
            """
            INSERT INTO candidate_profiles (profile_id, version, profile_json, created_at)
            VALUES ('legacy-candidate', 2, ?, '2026-07-01T00:00:00+00:00')
            """,
            (candidate_json,),
        )
        user_json = json.dumps(
            {
                "profile_id": "panel-profile",
                "version": 1,
                "target_roles": ["Evaluation Engineer"],
                "evidence": [{"id": "eval", "statement": "Evaluates", "source": "user"}],
                "location_rule": "Warsaw",
                "approved_at": "2026-07-02T00:00:00+00:00",
            }
        )
        connection.execute(
            """
            INSERT INTO user_profiles (
                profile_id, display_name, profile_json, status, created_at, updated_at
            ) VALUES (
                'panel-profile', 'Panel Profile', ?, 'ready',
                '2026-07-02T00:00:00+00:00', '2026-07-02T00:00:00+00:00'
            )
            """,
            (user_json,),
        )
        connection.execute(
            """
            INSERT INTO profile_documents (
                document_id, profile_id, original_filename, stored_path, sha256,
                content_type, extraction_method, extracted_text, page_count,
                character_count, created_at
            ) VALUES (
                'legacy-document', 'panel-profile', 'cv.pdf', '/tmp/cv.pdf', ?,
                'application/pdf', 'ocr', 'Legacy OCR text', 1, 15,
                '2026-07-02T00:00:00+00:00'
            )
            """,
            ("c" * 64,),
        )

    initialize_database(path)

    legacy = get_user_profile(path, "legacy-candidate")
    panel = get_user_profile(path, "panel-profile")
    assert legacy["current_version"] == 2
    assert legacy["profile"]["target_roles"] == ["AI Engineer"]
    assert panel["display_name"] == "Panel Profile"
    assert panel["document"]["extracted_text"] == "Legacy OCR text"
    assert panel["document"]["review_status"] == "needs_review"
    assert [item["version"] for item in list_profile_versions(path, "panel-profile")] == [1]
    with connect(path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 17


def test_reapplying_migrations_preserves_run_items_and_results(tmp_path):
    path = tmp_path / "current.db"
    initialize_database(path)
    items = _create_run_with_items(
        path, "completed-run", EvaluationItemStatus.COMPLETED, [EvaluationItemStatus.COMPLETED]
    )
    with connect(path) as connection:
        connection.execute(
            "UPDATE evaluation_run_items SET final_assessment_json = ? WHERE item_id = ?",
            ('{"final_score": 8.5}', items[0].item_id),
        )

    initialize_database(path)

    with connect(path) as connection:
        stored = connection.execute(
            "SELECT status, final_assessment_json FROM evaluation_run_items"
        ).fetchone()
        versions = connection.execute("SELECT count(*) FROM schema_version").fetchone()[0]
    assert stored["status"] == "completed"
    assert json.loads(stored["final_assessment_json"])["final_score"] == 8.5
    assert versions == 17


def test_profile_document_and_lab_configuration_are_persisted_locally(tmp_path):
    path = tmp_path / "job_scout.db"
    save_user_profile(
        path,
        profile_id="profile-1",
        display_name="Test Profile",
        profile_json={"target_roles": ["AI Engineer"], "evidence": []},
        status="ready",
    )
    save_profile_document(
        path,
        document_id="document-1",
        profile_id="profile-1",
        original_filename="cv.pdf",
        stored_path=tmp_path / "cv.pdf",
        sha256="a" * 64,
        extraction_method="ocr",
        extracted_text="CV text",
        page_count=1,
    )
    save_lab_model_profile(
        path,
        model_profile_id="model-1",
        display_name="Local Qwen",
        role="scout",
        model_path="models/qwen.gguf",
        config_json={"context_size": 4096, "temperature": 0, "seed": 42},
    )
    save_lab_experiment(
        path,
        experiment_id="experiment-1",
        display_name="Baseline",
        profile_id="profile-1",
        dataset_key="frozen-v1",
        task_kind="extraction",
        model_profile_id="model-1",
        config_json={"seed": 42},
    )
    assert get_user_profile(path, "profile-1")["document"]["extraction_method"] == "ocr"
    assert list_lab_model_profiles(path)[0]["display_name"] == "Local Qwen"
    assert list_lab_experiments(path)[0]["profile_name"] == "Test Profile"


def test_cv_review_creates_immutable_approved_profile_version(tmp_path):
    path = tmp_path / "job_scout.db"
    save_user_profile(
        path,
        profile_id="profile-review",
        display_name="Review Profile",
        profile_json={
            "profile_id": "profile-review",
            "version": 1,
            "target_roles": ["AI Engineer"],
            "evidence": [{"id": "python", "statement": "Uses Python", "source": "CV"}],
            "location_rule": "Remote from Poland",
            "approved_at": None,
        },
        status="cv_review",
    )
    save_profile_document(
        path,
        document_id="document-review",
        profile_id="profile-review",
        original_filename="cv.pdf",
        stored_path=tmp_path / "cv.pdf",
        sha256="b" * 64,
        extraction_method="ocr",
        extracted_text="OCR text with one typoo",
        page_count=1,
    )

    version = approve_profile_document(
        path,
        profile_id="profile-review",
        document_id="document-review",
        corrected_text="OCR text with one typo corrected",
        original_language="en",
    )
    stored = get_user_profile(path, "profile-review")
    versions = list_profile_versions(path, "profile-review")

    assert version == 2
    assert stored["status"] == "ready"
    assert stored["completeness_status"] == "ready_for_scoring"
    assert stored["profile"]["approved_at"] is not None
    assert stored["document"]["corrected_text"] == "OCR text with one typo corrected"
    assert stored["document"]["review_status"] == "approved"
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[0]["status"] == "approved"
    assert versions[1]["superseded_at"] is not None


def test_monitored_collection_marks_only_confirmed_missing_offers_unavailable(tmp_path):
    from job_scout.ats_scrapers import DiscoveredJob
    from job_scout.collector import SourceObservation

    path = tmp_path / "job_scout.db"
    initialize_database(path)
    first = clean_offer(external_id="first", url="https://example.com/jobs/first")
    second = clean_offer(external_id="second", url="https://example.com/jobs/second")
    with connect(path) as connection:
        persist_clean_offer(connection, first, source_url="https://example.com/careers")
        persist_clean_offer(connection, second, source_url="https://example.com/careers")

    observation = SourceObservation(
        source_id="example",
        company="Example AI",
        status="ok",
        discovered_count=1,
        selected_count=1,
        observed_jobs=[
            DiscoveredJob(
                source_id="example",
                company="Example AI",
                external_id="first",
                title="AI Engineer",
                url="https://example.com/jobs/first",
            )
        ],
    )
    summary = persist_monitored_collection(
        path,
        run_id="demo-v2-test",
        mode="sample",
        offers=[first],
        source_urls={"example": "https://example.com/careers"},
        observations=[observation],
    )
    with connect(path) as connection:
        statuses = {
            row["identity_key"]: row["availability_status"]
            for row in connection.execute("SELECT identity_key, availability_status FROM offers")
        }
    assert summary["offers_marked_unavailable"] == 1
    assert statuses["example:first"] == "active"
    assert statuses["example:second"] == "unavailable"
    disappeared = list_offer_events(path, event_type="disappeared")
    assert len(disappeared) == 1
    assert disappeared[0]["event_type"] == "disappeared"


def test_persist_clean_offer_deduplicates_same_raw_version(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    with connect(path) as connection:
        first_id, first_created = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers", seen_at=now
        )
        second_id, second_created = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers", seen_at=now
        )
        assert first_id == second_id
        assert first_created is True
        assert second_created is False
        assert connection.execute("SELECT count(*) FROM raw_offers").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM offers").fetchone()[0] == 1


def test_offer_language_and_versioned_translation_are_persisted(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    polish_text = (
        "Wymagania na stanowisku: doświadczenie w Python oraz praca z zespołem przez 3 lata."
    )
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection,
            clean_offer(description=polish_text, analysis_text=polish_text),
            source_url="https://example.com/careers",
        )
        detected = connection.execute(
            "SELECT original_language, language_confidence FROM offers WHERE id = ?",
            (offer_id,),
        ).fetchone()
    assert detected["original_language"] == "pl"
    assert detected["language_confidence"] > 0.5

    quote = "doświadczenie w Python"
    start = polish_text.index(quote)
    translation = CanonicalEnglishTranslation(
        translated_text=(
            "Role requirements: 3 years of Python experience and teamwork."
        ),
        quote_map=[
            TranslationQuoteMap(
                original_quote=quote,
                translated_quote="Python experience",
                source_start=start,
                source_end=start + len(quote),
            )
        ],
    )
    save_offer_translation(
        path,
        translation_id="translation-1",
        offer_id=offer_id,
        source_text=polish_text,
        source_language="pl",
        translation=translation,
        translator_model="translator-test",
        prompt_version="translation-v1",
    )
    approve_offer_translation(path, "translation-1")
    stored = get_latest_offer_translation(path, offer_id)
    assert stored["status"] == "approved"
    assert stored["translated"]["translated_text"].startswith("Role requirements")
    assert stored["quote_map"][0]["original_quote"] == quote


def test_translation_must_match_current_offer_version_and_language(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    polish_text = "Wymagania: praca z zespołem oraz doświadczenie w Python przez 3 lata."
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection,
            clean_offer(description=polish_text, analysis_text=polish_text),
            source_url="https://example.com/careers",
        )
    translation = CanonicalEnglishTranslation(
        translated_text="Requirements: 3 years of Python experience and teamwork.",
        quote_map=[
            TranslationQuoteMap(
                original_quote="doświadczenie w Python",
                translated_quote="Python experience",
                source_start=37,
                source_end=60,
            )
        ],
    )
    with pytest.raises(ValueError, match="current offer content"):
        save_offer_translation(
            path,
            translation_id="stale",
            offer_id=offer_id,
            source_text=polish_text + " changed",
            source_language="pl",
            translation=translation,
            translator_model="test",
            prompt_version="v1",
        )
    with pytest.raises(ValueError, match="source language"):
        save_offer_translation(
            path,
            translation_id="wrong-language",
            offer_id=offer_id,
            source_text=polish_text,
            source_language="en",
            translation=translation,
            translator_model="test",
            prompt_version="v1",
        )


def test_backfill_offer_languages_updates_existing_unknown_rows(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    text = "Wymagania na stanowisku: doświadczenie oraz praca z zespołem."
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection,
            clean_offer(description=text, analysis_text=text),
            source_url="https://example.com/careers",
        )
        connection.execute(
            "UPDATE offers SET original_language = 'unknown' WHERE id = ?", (offer_id,)
        )
    counts = backfill_offer_languages(path)
    assert counts["pl"] == 1
    with connect(path) as connection:
        language = connection.execute(
            "SELECT original_language FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()[0]
    assert language == "pl"


def test_backfill_offer_ledger_is_idempotent(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
        connection.execute("DELETE FROM offer_versions WHERE offer_id = ?", (offer_id,))
        connection.execute(
            "UPDATE offers SET current_content_sha256 = NULL WHERE id = ?", (offer_id,)
        )
    assert backfill_offer_ledger(path) == 1
    assert backfill_offer_ledger(path) == 0
    assert list_offer_versions(path, offer_id)[0]["change_kind"] == "imported"


def test_changed_payload_adds_raw_version_and_updates_clean_offer(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
        updated_id, created = persist_clean_offer(
            connection,
            clean_offer(
                title="Senior AI Engineer",
                raw_payload="<html>second version</html>",
                raw_sha256="b" * 64,
            ),
            source_url="https://example.com/careers",
        )
        row = connection.execute("SELECT title FROM offers WHERE id = ?", (offer_id,)).fetchone()
        assert updated_id == offer_id
        assert created is True
        assert row["title"] == "Senior AI Engineer"
        assert connection.execute("SELECT count(*) FROM raw_offers").fetchone()[0] == 2
    versions = list_offer_versions(path, offer_id)
    assert [version["change_kind"] for version in versions] == ["updated", "new"]
    assert versions[0]["changed_fields"] == ["title"]
    events = list_offer_events(path)
    assert [event["event_type"] for event in events] == ["updated", "new"]


def test_transport_only_raw_change_does_not_queue_reevaluation(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
        connection.execute(
            """
            UPDATE offers
            SET needs_evaluation = 0, evaluation_input_sha256 = current_content_sha256
            WHERE id = ?
            """,
            (offer_id,),
        )
        _, raw_created = persist_clean_offer(
            connection,
            clean_offer(raw_payload="<html>transport changed</html>", raw_sha256="c" * 64),
            source_url="https://example.com/careers",
        )
        state = connection.execute(
            "SELECT needs_evaluation FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()[0]
    assert raw_created is True
    assert state == 0
    assert len(list_offer_versions(path, offer_id)) == 1


def test_export_queue_is_idempotent_and_retains_failure_for_retry(tmp_path):
    path = tmp_path / "job_scout.db"
    export_id = enqueue_export(
        path,
        provider="notion",
        entity_type="offer",
        entity_id="42",
        content_sha256="a" * 64,
    )
    assert (
        enqueue_export(
            path,
            provider="notion",
            entity_type="offer",
            entity_id="42",
            content_sha256="a" * 64,
        )
        == export_id
    )
    record_export_result(path, export_id, success=False, error="temporary outage")
    pending = list_pending_exports(path, "notion")
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_error"] == "temporary outage"
    record_export_result(path, export_id, success=True)
    assert list_pending_exports(path, "notion") == []


def test_offer_event_creates_private_in_app_notification(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
    notifications = list_notifications(path, unread_only=True)
    assert len(notifications) == 1
    assert "Example AI" in notifications[0]["title"]
    assert notifications[0]["private_link"] == f"/offers/{offer_id}"
    assert "Build AI products" not in notifications[0]["body"]
    assert mark_notification_read(path, notifications[0]["notification_id"]) is True
    assert list_notifications(path, unread_only=True) == []


def test_web_push_subscription_and_delivery_outbox_are_deduplicated(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
    notification_id = list_notifications(path)[0]["notification_id"]
    subscription_id = save_web_push_subscription(
        path,
        endpoint="https://push.example.test/subscription/1",
        p256dh="public-key",
        auth="auth-secret",
        user_agent="test",
    )
    first = queue_notification_delivery(
        path,
        notification_id=notification_id,
        channel="web_push",
        destination_key=subscription_id,
    )
    second = queue_notification_delivery(
        path,
        notification_id=notification_id,
        channel="web_push",
        destination_key=subscription_id,
    )
    assert first == second
    with connect(path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM notification_deliveries"
            ).fetchone()[0]
        )
    assert set(payload) == {"title", "body", "link"}
    assert "profile" not in json.dumps(payload).casefold()


def test_raw_payload_is_excluded_from_clean_json():
    serialized = clean_offer().model_dump_json()
    assert "first version" not in serialized
    assert "raw_payload" not in serialized


def test_repair_offer_text_markup_removes_legacy_html_and_queues_re_evaluation(tmp_path):
    from job_scout.storage import repair_offer_text_markup

    path = tmp_path / "job_scout.db"
    initialize_database(path)
    raw_description = "<p><span>Build AI agents.</span></p><ul><li>Evaluate models.</li></ul>"
    raw_offer = clean_offer(description=raw_description, analysis_text=raw_description)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, raw_offer, source_url="https://example.com/careers"
        )
        # Simulate a record written by a pre-cleaning version of the application.
        legacy_payload = raw_offer.model_dump(
            mode="json", exclude={"raw_payload", "raw_content_type"}
        )
        connection.execute(
            "UPDATE offers SET offer_json=?, needs_evaluation=0, assessment_json='{}' WHERE id=?",
            (json.dumps(legacy_payload), offer_id),
        )

    assert repair_offer_text_markup(path) == 1
    repaired = get_offer(path, offer_id)
    assert repaired is not None
    assert repaired["offer"]["description"] == "Build AI agents. Evaluate models."
    assert "<span>" not in repaired["offer"]["analysis_text"]
    assert repaired["needs_evaluation"] == 1
    assert repaired["assessment_json"] is None
    assert repair_offer_text_markup(path) == 0


def test_evaluation_run_requires_approved_profile_and_persists_snapshot(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
    run = PipelineRun(
        run_id="demo-1",
        source_id="demo",
        total_items=1,
        profile_id="synthetic-candidate-v2",
        profile_version=1,
    )
    item = EvaluationRunItem(
        item_id="demo-1:1",
        run_id="demo-1",
        offer_id=offer_id,
        input_sha256="f" * 64,
        input_snapshot={"analysis_text": "Build AI products"},
    )
    with pytest.raises(ValueError, match="must be approved"):
        create_evaluation_run(path, run, candidate_profile(approved=False), [item])

    create_evaluation_run(path, run, candidate_profile(), [item])
    with connect(path) as connection:
        stored_run = connection.execute(
            "SELECT profile_json, total_items FROM pipeline_runs WHERE run_id = 'demo-1'"
        ).fetchone()
        stored_item = connection.execute(
            "SELECT input_snapshot_json FROM evaluation_run_items WHERE item_id = 'demo-1:1'"
        ).fetchone()
    assert json.loads(stored_run["profile_json"])["profile_id"] == "synthetic-candidate-v2"
    assert stored_run["total_items"] == 1
    assert json.loads(stored_item["input_snapshot_json"])["analysis_text"] == (
        "Build AI products"
    )


def test_cancel_run_is_atomic_and_preserves_terminal_items(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_run_with_items(
        path,
        "cancel-run",
        EvaluationItemStatus.RUNNING,
        [
            EvaluationItemStatus.PENDING,
            EvaluationItemStatus.RUNNING,
            EvaluationItemStatus.COMPLETED,
            EvaluationItemStatus.FAILED,
        ],
    )

    assert cancel_run(path, "cancel-run") is True

    run = get_pipeline_run(path, "cancel-run")
    items = list_evaluation_run_items(path, "cancel-run")
    assert run["status"] == "cancelled"
    assert run["current_stage"] == "cancelled"
    assert run["finished_at"] is not None
    assert run["completed_items"] == 2
    assert sorted(item["status"] for item in items) == [
        "cancelled",
        "cancelled",
        "completed",
        "failed",
    ]
    assert cancel_run(path, "cancel-run") is False


def test_resume_run_resets_only_resumable_items_and_run_metadata(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_run_with_items(
        path,
        "resume-run",
        EvaluationItemStatus.CANCELLED,
        [
            EvaluationItemStatus.CANCELLED,
            EvaluationItemStatus.INTERRUPTED,
            EvaluationItemStatus.RUNNING,
            EvaluationItemStatus.COMPLETED,
            EvaluationItemStatus.FAILED,
        ],
    )
    with connect(path) as connection:
        connection.execute(
            """
            UPDATE evaluation_run_items
            SET extracted_json = '{"kept": true}', draft_json = '{"stale": true}',
                judgment_json = '{"stale": true}', final_assessment_json = '{"stale": true}',
                timings_json = '{"evaluator": 10}', retry_counts_json = '{"evaluator": 1}',
                error = 'stale item error'
            WHERE status IN ('cancelled', 'interrupted', 'running')
            """
        )

    resume_run(path, "resume-run")

    run = get_pipeline_run(path, "resume-run")
    items = list_evaluation_run_items(path, "resume-run")
    reset_items = [item for item in items if item["status"] == "pending"]
    assert run["status"] == "running"
    assert run["current_stage"] == "starting"
    assert run["finished_at"] is None
    assert run["error"] is None
    assert run["completed_items"] == 2
    assert len(reset_items) == 3
    assert all(item["extracted"] == {"kept": True} for item in reset_items)
    assert all(item["draft"] is None and item["judgment"] is None for item in reset_items)
    assert all(item["final_assessment"] is None for item in reset_items)
    assert all(item["timings"] == {} and item["retry_counts"] == {} for item in reset_items)
    assert all(item["error"] is None for item in reset_items)
    assert {item["status"] for item in items if item["status"] != "pending"} == {
        "completed",
        "failed",
    }


def test_resume_run_rejects_non_resumable_status(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_run_with_items(
        path, "complete-run", EvaluationItemStatus.COMPLETED, [EvaluationItemStatus.COMPLETED]
    )
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume_run(path, "complete-run")


def test_mark_interrupted_runs_records_stage_and_timestamp(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_run_with_items(
        path,
        "interrupt-run",
        EvaluationItemStatus.RUNNING,
        [EvaluationItemStatus.RUNNING, EvaluationItemStatus.PENDING],
    )

    mark_interrupted_runs(path)

    run = get_pipeline_run(path, "interrupt-run")
    statuses = sorted(
        item["status"] for item in list_evaluation_run_items(path, "interrupt-run")
    )
    assert run["status"] == "interrupted"
    assert run["current_stage"] == "interrupted"
    assert run["finished_at"] is not None
    assert statuses == ["interrupted", "pending"]


def test_completed_items_recalculation_is_idempotent(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    item = _create_run_with_items(
        path, "progress-run", EvaluationItemStatus.RUNNING, [EvaluationItemStatus.PENDING]
    )[0]
    item.status = EvaluationItemStatus.COMPLETED

    update_evaluation_run_item(path, item, is_completed=True)
    update_evaluation_run_item(path, item, is_completed=True)

    assert get_pipeline_run(path, "progress-run")["completed_items"] == 1


def test_restore_backup_drill_preserves_core_counts_and_migrates_copy(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    restored = tmp_path / "restore" / "restored.db"
    initialize_database(source)
    save_user_profile(
        source,
        profile_id="restore-profile",
        display_name="Restore profile",
        profile_json={"profile_id": "restore-profile", "version": 1},
        status="draft",
    )
    with connect(source) as connection:
        persist_clean_offer(connection, clean_offer(), source_url="https://example.com/careers")
    with sqlite3.connect(source) as source_connection, sqlite3.connect(backup) as backup_connection:
        source_connection.backup(backup_connection)

    result = restore_backup_drill(backup, restored)

    assert result == {"offers": 1, "profiles": 1, "runs": 0, "events": 1}
    with connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
