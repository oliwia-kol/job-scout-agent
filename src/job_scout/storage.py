"""SQLite persistence. SQLite is the MVP source of truth."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from .language import (
    CanonicalEnglishTranslation,
    detect_language,
    validate_canonical_translation,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .ats_scrapers import CleanJob
    from .collector import SourceObservation
    from .domain import CandidateProfile, EvaluationRunItem, PipelineRun


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _execute_sql_file(connection: sqlite3.Connection, path: Path) -> None:
    """Execute a migration file without sqlite3.executescript's implicit commit."""
    statement = ""
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError(f"incomplete SQL statement in {path.name}")


def _add_missing_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = _column_names(connection, table)
    for column, definition in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_legacy_database(connection: sqlite3.Connection, migrations_dir: Path) -> None:
    """Upgrade pre-versioned and partially upgraded MVP databases safely."""
    _execute_sql_file(connection, migrations_dir / "001_initial.sql")
    _add_missing_columns(
        connection,
        "raw_offers",
        {
            "source_url": "TEXT NOT NULL DEFAULT ''",
            "identity_key": "TEXT",
            "title_hint": "TEXT",
        },
    )
    _add_missing_columns(connection, "offers", {"identity_key": "TEXT"})
    _add_missing_columns(
        connection,
        "pipeline_runs",
        {
            "run_type": "TEXT NOT NULL DEFAULT 'collection'",
            "total_items": "INTEGER NOT NULL DEFAULT 0",
            "completed_items": "INTEGER NOT NULL DEFAULT 0",
            "current_stage": "TEXT",
            "profile_id": "TEXT",
            "profile_version": "INTEGER",
            "profile_json": "TEXT",
            "prompt_versions_json": "TEXT NOT NULL DEFAULT '{}'",
            "mlflow_run_id": "TEXT",
            "models_json": "TEXT NOT NULL DEFAULT '{}'",
        },
    )
    connection.execute(
        """
        UPDATE raw_offers
        SET identity_key = source_id || ':' ||
            COALESCE(NULLIF(external_id, ''), RTRIM(job_url, '/'))
        WHERE identity_key IS NULL
        """
    )
    connection.execute(
        """
        UPDATE offers
        SET identity_key = source_id || ':' || RTRIM(job_url, '/')
        WHERE identity_key IS NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS raw_offer_version_identity
        ON raw_offers(identity_key, payload_sha256)
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS offer_current_identity ON offers(identity_key)"
    )


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    migrations_dir = Path(__file__).parent / "migrations"
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        current_version = row["version"] if row and row["version"] is not None else 0

        if current_version == 0:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if tables - {"schema_version"}:
                _migrate_legacy_database(connection, migrations_dir)
            else:
                _execute_sql_file(connection, migrations_dir / "001_initial.sql")
            connection.execute("INSERT INTO schema_version (version) VALUES (1)")
            current_version = 1

        for version, filename in [
            (2, "002_add_profiles_and_diagnostics.sql"),
            (3, "003_add_run_parent.sql"),
            (4, "004_add_collection_monitoring.sql"),
            (5, "005_add_profiles_and_llm_lab.sql"),
            (6, "006_unify_profile_versions.sql"),
            (7, "007_add_career_interview.sql"),
            (8, "008_add_offer_languages.sql"),
            (9, "009_add_offer_ledger.sql"),
            (10, "010_add_offer_copilot.sql"),
            (11, "011_add_notifications.sql"),
            (12, "012_add_voice_transcripts.sql"),
            (13, "013_add_app_guide_chat.sql"),
            (14, "014_add_product_run_details.sql"),
            (15, "015_add_profile_facts.sql"),
            (16, "016_add_offer_section_evidence.sql"),
            (17, "017_add_cv_tailoring.sql"),
        ]:
            if current_version < version:
                _execute_sql_file(connection, migrations_dir / filename)
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
                current_version = version


def initialize_database(path: Path) -> None:
    with connect(path) as connection:
        apply_migrations(connection)


def restore_backup_drill(backup_path: Path, restored_path: Path) -> dict[str, int]:
    """Restore a SQLite backup into a separate database and verify its essential state."""
    if not backup_path.is_file():
        raise ValueError(f"backup does not exist: {backup_path}")
    if backup_path.resolve() == restored_path.resolve():
        raise ValueError("restore target must differ from the backup")
    restored_path.parent.mkdir(parents=True, exist_ok=True)
    tables = ("offers", "user_profiles", "pipeline_runs", "offer_events")
    with sqlite3.connect(backup_path) as source, sqlite3.connect(restored_path) as restored:
        source.backup(restored)
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"restored backup integrity check failed: {integrity}")
        source_counts = {
            table: int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
    initialize_database(restored_path)
    with connect(restored_path) as restored:
        restored_counts = {
            table: int(restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        if restored_counts != source_counts:
            raise ValueError("restored data counts differ from the backup")
    return {
        "offers": restored_counts["offers"],
        "profiles": restored_counts["user_profiles"],
        "runs": restored_counts["pipeline_runs"],
        "events": restored_counts["offer_events"],
    }


def offer_identity(source_id: str, external_id: str | None, job_url: str) -> str:
    """Stable source-scoped identity; external ATS ids take precedence over URLs."""
    stable_part = (
        external_id.strip() if external_id and external_id.strip() else job_url.rstrip("/")
    )
    return f"{source_id}:{stable_part}"


def offer_model_content_hash(offer: CleanJob) -> str:
    """Hash normalized, model-relevant fields instead of transport metadata."""
    payload = {
        "title": offer.title,
        "locations": offer.locations,
        "analysis_text": offer.analysis_text,
        "supplemental_info": offer.supplemental_info.model_dump(mode="json"),
        "employment_type": offer.employment_type,
        "published_at": offer.published_at,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()


def persist_clean_offer(
    connection: sqlite3.Connection,
    offer: CleanJob,
    *,
    source_url: str,
    seen_at: datetime | None = None,
    collection_run_id: str | None = None,
) -> tuple[int, bool]:
    """Store one raw version and upsert the latest cleaned representation.

    Returns ``(offer_id, raw_version_created)``.
    """
    # Callers include browser/API collectors, frozen demo data and maintenance imports.
    # Never let a bypassed collector persist markup into the user-facing or model-facing fields.
    from .ats_scrapers import normalize_clean_job

    offer = normalize_clean_job(offer)
    timestamp = (seen_at or datetime.now(UTC)).isoformat()
    job_url = str(offer.url)
    identity_key = offer_identity(offer.source_id, offer.external_id, job_url)
    previous = connection.execute(
        """
        SELECT id, offer_json, availability_status, current_content_sha256
        FROM offers WHERE identity_key = ?
        """,
        (identity_key,),
    ).fetchone()
    raw_created = False
    if offer.raw_payload:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO raw_offers (
                source_id, company, source_url, job_url, identity_key, external_id,
                title_hint, payload, content_type, fetched_at, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                offer.source_id,
                offer.company,
                source_url,
                job_url,
                identity_key,
                offer.external_id,
                offer.title,
                offer.raw_payload,
                offer.raw_content_type,
                timestamp,
                offer.raw_sha256,
            ),
        )
        raw_created = cursor.rowcount == 1
    clean_json = offer.model_dump_json(exclude={"raw_payload", "raw_content_type"})
    content_hash = offer_model_content_hash(offer)
    language = detect_language(f"{offer.title}\n{offer.description}")
    connection.execute(
        """
        INSERT INTO offers (
            source_id, identity_key, company, title, job_url, offer_json,
            first_seen_at, last_seen_at, availability_status, last_checked_at, disappeared_at,
            original_language, language_confidence, language_detected_at,
            current_content_sha256, needs_evaluation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?, 1)
        ON CONFLICT(identity_key) DO UPDATE SET
            company=excluded.company,
            title=excluded.title,
            job_url=excluded.job_url,
            offer_json=excluded.offer_json,
            last_seen_at=excluded.last_seen_at,
            availability_status='active',
            last_checked_at=excluded.last_checked_at,
            disappeared_at=NULL,
            original_language=excluded.original_language,
            language_confidence=excluded.language_confidence,
            language_detected_at=excluded.language_detected_at,
            current_content_sha256=excluded.current_content_sha256,
            needs_evaluation=CASE
                WHEN offers.current_content_sha256 IS NULL
                  OR offers.current_content_sha256 != excluded.current_content_sha256
                THEN 1 ELSE offers.needs_evaluation END
        """,
        (
            offer.source_id,
            identity_key,
            offer.company,
            offer.title,
            job_url,
            clean_json,
            timestamp,
            timestamp,
            timestamp,
            language.language,
            language.confidence,
            timestamp,
            content_hash,
        ),
    )
    row = connection.execute(
        "SELECT id FROM offers WHERE identity_key = ?", (identity_key,)
    ).fetchone()
    offer_id = int(row["id"])
    old_payload = json.loads(previous["offer_json"]) if previous else None
    changed_fields = _changed_offer_fields(old_payload, json.loads(clean_json))
    content_changed = previous is None or previous["current_content_sha256"] != content_hash
    reappeared = bool(previous and previous["availability_status"] == "unavailable")
    if content_changed:
        change_kind = "new" if previous is None else "updated"
        version_id = sha256(f"{offer_id}:{content_hash}".encode()).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO offer_versions (
                version_id, offer_id, content_sha256, offer_json, change_kind,
                changed_fields_json, collection_run_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                offer_id,
                content_hash,
                clean_json,
                change_kind,
                json.dumps(changed_fields, ensure_ascii=False),
                collection_run_id,
                timestamp,
            ),
        )
        _insert_offer_event(
            connection,
            offer_id=offer_id,
            event_type=change_kind,
            payload={"changed_fields": changed_fields, "content_sha256": content_hash},
            collection_run_id=collection_run_id,
            created_at=timestamp,
        )
        _persist_offer_section_evidence(
            connection,
            offer_id=offer_id,
            content_sha256=content_hash,
            offer=offer,
            created_at=timestamp,
        )
    elif reappeared:
        _insert_offer_event(
            connection,
            offer_id=offer_id,
            event_type="reappeared",
            payload={"content_sha256": content_hash},
            collection_run_id=collection_run_id,
            created_at=timestamp,
        )
    return offer_id, raw_created


def _persist_offer_section_evidence(
    connection: sqlite3.Connection,
    *,
    offer_id: int,
    content_sha256: str,
    offer: CleanJob,
    created_at: str,
) -> None:
    """Persist only deterministic, quoted findings for this exact offer version."""
    from .ats_scrapers import extract_role_sections, extract_supplemental_info

    responsibilities, requirements = extract_role_sections(offer.description)
    records: list[tuple[str, str | None, dict, str, str, float]] = []
    if responsibilities:
        records.append(
            (
                "responsibilities",
                None,
                {"text": responsibilities},
                responsibilities,
                "labelled_section_v1",
                1.0,
            )
        )
    if requirements:
        records.append(
            (
                "requirements",
                None,
                {"text": requirements},
                requirements,
                "labelled_section_v1",
                1.0,
            )
        )
    supplemental = extract_supplemental_info(offer.description)
    for section_kind, values in (
        ("work_conditions", supplemental.work_conditions),
        ("compensation", supplemental.compensation),
        ("benefits", supplemental.interesting_benefits),
        ("travel", supplemental.travel_requirements),
    ):
        records.extend(
            (
                section_kind,
                item.category,
                {"text": item.evidence},
                item.evidence,
                "pattern_v1",
                0.9,
            )
            for item in values
        )
    for section_kind, category, value, quote, method, confidence in records:
        evidence_id = sha256(
            f"{offer_id}:{content_sha256}:{section_kind}:{category}:{quote}".encode()
        ).hexdigest()
        connection.execute(
            """INSERT OR IGNORE INTO offer_section_evidence (
            evidence_id, offer_id, content_sha256, section_kind, category, value_json,
            source_quote, extraction_method, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id,
                offer_id,
                content_sha256,
                section_kind,
                category,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                quote,
                method,
                confidence,
                created_at,
            ),
        )


def _changed_offer_fields(previous: dict | None, current: dict) -> list[str]:
    if previous is None:
        return sorted(current)
    model_fields = (
        "title",
        "locations",
        "analysis_text",
        "supplemental_info",
        "employment_type",
        "published_at",
    )
    return [field for field in model_fields if previous.get(field) != current.get(field)]


def _insert_offer_event(
    connection: sqlite3.Connection,
    *,
    offer_id: int,
    event_type: str,
    payload: dict,
    collection_run_id: str | None,
    created_at: str,
) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    event_id = sha256(
        f"{offer_id}:{event_type}:{created_at}:{serialized}".encode()
    ).hexdigest()
    connection.execute(
        """
        INSERT OR IGNORE INTO offer_events (
            event_id, offer_id, event_type, event_json, collection_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, offer_id, event_type, serialized, collection_run_id, created_at),
    )
    offer = connection.execute(
        "SELECT company, title FROM offers WHERE id = ?", (offer_id,)
    ).fetchone()
    notification_id = sha256(f"{event_id}:in_app".encode()).hexdigest()
    labels = {
        "new": ("Nowa oferta", "Pojawiła się w monitorowanym źródle."),
        "updated": ("Oferta zmieniona", "Treść oferty została zaktualizowana."),
        "disappeared": (
            "Oferta zniknęła",
            "Nie występuje w ostatnim poprawnym odczycie źródła.",
        ),
        "reappeared": ("Oferta wróciła", "Ponownie występuje w źródle."),
    }
    title, body = labels.get(event_type, ("Zmiana oferty", "Zarejestrowano zmianę."))
    connection.execute(
        """
        INSERT OR IGNORE INTO notifications (
            notification_id, event_id, kind, title, body, private_link, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            notification_id,
            event_id,
            event_type,
            f"{title}: {offer['company']} — {offer['title']}",
            body,
            f"/offers/{offer_id}",
            created_at,
        ),
    )


def save_offer_translation(
    path: Path,
    *,
    translation_id: str,
    offer_id: int,
    source_text: str,
    source_language: str,
    translation: CanonicalEnglishTranslation,
    translator_model: str,
    prompt_version: str,
) -> None:
    if source_language not in {"pl", "en", "mixed"}:
        raise ValueError("translation source language must be pl, en or mixed")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    source_hash = sha256(source_text.encode("utf-8")).hexdigest()
    with connect(path) as connection:
        offer_row = connection.execute(
            "SELECT offer_json, original_language FROM offers WHERE id = ?",
            (offer_id,),
        ).fetchone()
        if not offer_row:
            raise ValueError("offer not found")
        offer_payload = json.loads(offer_row["offer_json"])
        current_source_text = (
            offer_payload.get("analysis_text") or offer_payload.get("description") or ""
        )
        if source_text != current_source_text:
            raise ValueError("translation source does not match the current offer content")
        if offer_row["original_language"] != source_language:
            raise ValueError("translation source language does not match the current offer")
        validate_canonical_translation(source_text, translation)
        connection.execute(
            """
            INSERT INTO offer_translations (
                translation_id, offer_id, source_content_sha256, source_language,
                target_language, translated_json, quote_map_json, translator_model,
                prompt_version, status, created_at
            ) VALUES (?, ?, ?, ?, 'en', ?, ?, ?, ?, 'draft', ?)
            """,
            (
                translation_id,
                offer_id,
                source_hash,
                source_language,
                json.dumps(
                    {"translated_text": translation.translated_text},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    [item.model_dump(mode="json") for item in translation.quote_map],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                translator_model,
                prompt_version,
                timestamp,
            ),
        )


def approve_offer_translation(path: Path, translation_id: str) -> None:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        row = connection.execute(
            "SELECT status FROM offer_translations WHERE translation_id = ?",
            (translation_id,),
        ).fetchone()
        if not row or row["status"] != "draft":
            raise ValueError("draft offer translation not found")
        connection.execute(
            """
            UPDATE offer_translations SET status = 'approved', approved_at = ?
            WHERE translation_id = ?
            """,
            (timestamp, translation_id),
        )


def get_latest_offer_translation(path: Path, offer_id: int) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT * FROM offer_translations
            WHERE offer_id = ? AND target_language = 'en'
            ORDER BY status = 'approved' DESC, created_at DESC LIMIT 1
            """,
            (offer_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["translated"] = json.loads(result["translated_json"])
    result["quote_map"] = json.loads(result["quote_map_json"])
    return result


def list_offer_versions(path: Path, offer_id: int) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM offer_versions WHERE offer_id = ?
            ORDER BY created_at DESC
            """,
            (offer_id,),
        ).fetchall()
    return [
        {
            **dict(row),
            "offer": json.loads(row["offer_json"]),
            "changed_fields": json.loads(row["changed_fields_json"]),
        }
        for row in rows
    ]


def list_offer_events(
    path: Path,
    *,
    event_type: str = "",
    limit: int = 100,
) -> list[dict]:
    initialize_database(path)
    where = "WHERE event_type = ?" if event_type else ""
    params: tuple = (event_type, limit) if event_type else (limit,)
    with connect(path) as connection:
        rows = connection.execute(
            f"""
            SELECT event_id, offer_events.offer_id, event_type, event_json,
                   collection_run_id, offer_events.created_at, read_at,
                   offers.company, offers.title, offers.availability_status
            FROM offer_events
            JOIN offers ON offers.id = offer_events.offer_id
            {where}
            ORDER BY offer_events.created_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [{**dict(row), "event": json.loads(row["event_json"])} for row in rows]


def enqueue_export(
    path: Path,
    *,
    provider: str,
    entity_type: str,
    entity_id: str,
    content_sha256: str,
) -> str:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    export_id = sha256(
        f"{provider}:{entity_type}:{entity_id}:{content_sha256}".encode()
    ).hexdigest()
    with connect(path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO export_queue (
                export_id, provider, entity_type, entity_id, content_sha256,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                export_id,
                provider,
                entity_type,
                entity_id,
                content_sha256,
                timestamp,
                timestamp,
            ),
        )
    return export_id


def list_pending_exports(path: Path, provider: str, limit: int = 100) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM export_queue
            WHERE provider = ? AND status IN ('pending', 'failed')
            ORDER BY created_at LIMIT ?
            """,
            (provider, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def record_export_result(
    path: Path,
    export_id: str,
    *,
    success: bool,
    error: str | None = None,
) -> None:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE export_queue
            SET status = ?, attempts = attempts + 1, last_error = ?,
                updated_at = ?, sent_at = CASE WHEN ? THEN ? ELSE sent_at END
            WHERE export_id = ?
            """,
            (
                "sent" if success else "failed",
                None if success else error,
                timestamp,
                int(success),
                timestamp,
                export_id,
            ),
        )
    if cursor.rowcount != 1:
        raise ValueError("export queue item not found")


def list_notifications(path: Path, *, unread_only: bool = False, limit: int = 100) -> list[dict]:
    initialize_database(path)
    where = "WHERE read_at IS NULL" if unread_only else ""
    with connect(path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM notifications {where}
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_notification_read(path: Path, notification_id: str) -> bool:
    initialize_database(path)
    with connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE notifications SET read_at = COALESCE(read_at, ?)
            WHERE notification_id = ?
            """,
            (datetime.now(UTC).isoformat(), notification_id),
        )
    return cursor.rowcount == 1


def save_web_push_subscription(
    path: Path,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> str:
    if not endpoint.startswith("https://") or not p256dh or not auth:
        raise ValueError("valid Web Push subscription required")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    subscription_id = sha256(endpoint.encode()).hexdigest()
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO web_push_subscriptions (
                subscription_id, endpoint, p256dh, auth, user_agent, created_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh=excluded.p256dh, auth=excluded.auth,
                user_agent=excluded.user_agent, revoked_at=NULL
            """,
            (subscription_id, endpoint, p256dh, auth, user_agent, timestamp),
        )
    return subscription_id


def queue_notification_delivery(
    path: Path,
    *,
    notification_id: str,
    channel: str,
    destination_key: str,
) -> str:
    if channel not in {"web_push", "telegram", "email"}:
        raise ValueError("unsupported notification channel")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        notification = connection.execute(
            """
            SELECT title, body, private_link FROM notifications
            WHERE notification_id = ?
            """,
            (notification_id,),
        ).fetchone()
        if not notification:
            raise ValueError("notification not found")
        payload = {
            "title": notification["title"],
            "body": notification["body"],
            "link": notification["private_link"],
        }
        delivery_id = sha256(
            f"{notification_id}:{channel}:{destination_key}".encode()
        ).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_deliveries (
                delivery_id, notification_id, channel, destination_key,
                payload_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                delivery_id,
                notification_id,
                channel,
                destination_key,
                json.dumps(payload, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
    return delivery_id


def backfill_offer_languages(path: Path) -> dict[str, int]:
    """Detect language for stored offers without changing their source content."""
    initialize_database(path)
    counts = {"pl": 0, "en": 0, "mixed": 0, "unknown": 0}
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        rows = connection.execute("SELECT id, title, offer_json FROM offers").fetchall()
        for row in rows:
            payload = json.loads(row["offer_json"])
            description = payload.get("description") or payload.get("analysis_text") or ""
            detection = detect_language(f"{row['title']}\n{description}")
            connection.execute(
                """
                UPDATE offers
                SET original_language = ?, language_confidence = ?, language_detected_at = ?
                WHERE id = ?
                """,
                (detection.language, detection.confidence, timestamp, row["id"]),
            )
            counts[detection.language] += 1
    return counts


def repair_offer_text_markup(path: Path) -> int:
    """Repair legacy records whose cleaned fields still contain literal HTML.

    This is a local data-quality migration, not a source change: it does not generate
    offer-change notifications or overwrite raw payload history. Existing assessments are
    cleared because their model input may have included the corrupted text.
    """
    from .ats_scrapers import CleanJob, normalize_clean_job

    initialize_database(path)
    repaired = 0
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT id, offer_json, current_content_sha256 FROM offers"
        ).fetchall()
        for row in rows:
            offer = CleanJob.model_validate_json(row["offer_json"])
            normalized = normalize_clean_job(offer)
            if normalized == offer:
                continue
            clean_json = normalized.model_dump_json(exclude={"raw_payload", "raw_content_type"})
            content_hash = offer_model_content_hash(normalized)
            language = detect_language(f"{normalized.title}\n{normalized.description}")
            connection.execute(
                """
                UPDATE offers
                SET offer_json=?, current_content_sha256=?, needs_evaluation=1,
                    assessment_json=NULL, judge_json=NULL,
                    original_language=?, language_confidence=?, language_detected_at=?
                WHERE id=?
                """,
                (
                    clean_json,
                    content_hash,
                    language.language,
                    language.confidence,
                    datetime.now(UTC).isoformat(),
                    row["id"],
                ),
            )
            repaired += 1
    return repaired


def backfill_offer_ledger(path: Path) -> int:
    """Create a silent baseline version for offers stored before ledger migration."""
    from .ats_scrapers import CleanJob

    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    created = 0
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT id, offer_json, current_content_sha256 FROM offers"
        ).fetchall()
        for row in rows:
            offer = CleanJob.model_validate_json(row["offer_json"])
            content_hash = offer_model_content_hash(offer)
            connection.execute(
                """
                UPDATE offers SET current_content_sha256 = ?
                WHERE id = ? AND current_content_sha256 IS NULL
                """,
                (content_hash, row["id"]),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO offer_versions (
                    version_id, offer_id, content_sha256, offer_json, change_kind,
                    changed_fields_json, collection_run_id, created_at
                ) VALUES (?, ?, ?, ?, 'imported', '[]', NULL, ?)
                """,
                (
                    sha256(f"{row['id']}:{content_hash}".encode()).hexdigest(),
                    row["id"],
                    content_hash,
                    row["offer_json"],
                    timestamp,
                ),
            )
            created += int(cursor.rowcount == 1)
    return created


def persist_monitored_collection(
    path: Path,
    *,
    run_id: str,
    mode: str,
    offers: Iterable[CleanJob],
    source_urls: dict[str, str],
    observations: Iterable[SourceObservation],
    started_at: datetime | None = None,
) -> dict[str, int | str]:
    """Persist a collection plus source availability without deleting historical offers.

    An offer is marked unavailable only after a successful complete discovery for its source.
    Empty and failed sources deliberately leave existing availability unchanged: they are not
    reliable proof that a job was removed from the employer's page.
    """
    initialize_database(path)
    began = started_at or datetime.now(UTC)
    finished = datetime.now(UTC)
    offers_list = list(offers)
    observations_list = list(observations)
    saved = new_versions = unavailable = 0
    healthy = sum(observation.status == "ok" for observation in observations_list)
    status = "completed" if healthy == len(observations_list) else "partial"
    error = None if status == "completed" else "one or more sources were empty or unavailable"

    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO collection_runs (
                run_id, mode, started_at, finished_at, status, sources_total, sources_ok,
                offers_saved, new_raw_versions, offers_marked_unavailable, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                mode,
                began.isoformat(),
                finished.isoformat(),
                status,
                len(observations_list),
                healthy,
                0,
                0,
                0,
                error,
            ),
        )
        for observation in observations_list:
            connection.execute(
                """
                INSERT INTO collection_source_runs (
                    run_id, source_id, company, status, discovered_count, selected_count,
                    error, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    observation.source_id,
                    observation.company,
                    observation.status,
                    observation.discovered_count,
                    observation.selected_count,
                    observation.error,
                    finished.isoformat(),
                ),
            )
            if observation.status != "ok":
                continue
            identities = {
                offer_identity(job.source_id, job.external_id, str(job.url))
                for job in observation.observed_jobs
            }
            if identities:
                placeholders = ", ".join("?" for _ in identities)
                missing_rows = connection.execute(
                    f"""
                    SELECT id, current_content_sha256 FROM offers
                    WHERE source_id=? AND availability_status='active'
                      AND identity_key NOT IN ({placeholders})
                    """,
                    (observation.source_id, *identities),
                ).fetchall()
                for missing in missing_rows:
                    connection.execute(
                        """
                        UPDATE offers
                        SET availability_status='unavailable',
                            disappeared_at=COALESCE(disappeared_at, ?),
                            last_checked_at=?, last_collection_run_id=?
                        WHERE id=?
                        """,
                        (
                            finished.isoformat(),
                            finished.isoformat(),
                            run_id,
                            missing["id"],
                        ),
                    )
                    _insert_offer_event(
                        connection,
                        offer_id=missing["id"],
                        event_type="disappeared",
                        payload={"content_sha256": missing["current_content_sha256"]},
                        collection_run_id=run_id,
                        created_at=finished.isoformat(),
                    )
                unavailable += len(missing_rows)
            connection.execute(
                """
                UPDATE offers SET last_checked_at=?, last_collection_run_id=?
                WHERE source_id=?
                """,
                (finished.isoformat(), run_id, observation.source_id),
            )

        for offer in offers_list:
            _, created = persist_clean_offer(
                connection,
                offer,
                source_url=source_urls[offer.source_id],
                seen_at=finished,
                collection_run_id=run_id,
            )
            saved += 1
            new_versions += int(created)
            connection.execute(
                "UPDATE offers SET last_collection_run_id=? WHERE identity_key=?",
                (run_id, offer_identity(offer.source_id, offer.external_id, str(offer.url))),
            )

        connection.execute(
            """
            UPDATE collection_runs
            SET offers_saved=?, new_raw_versions=?, offers_marked_unavailable=?
            WHERE run_id=?
            """,
            (saved, new_versions, unavailable, run_id),
        )
    return {
        "run_id": run_id,
        "status": status,
        "offers_saved": saved,
        "new_raw_versions": new_versions,
        "offers_marked_unavailable": unavailable,
        "sources_total": len(observations_list),
        "sources_ok": healthy,
    }


def save_collection_rejections(
    path: Path, run_id: str, rejected: Iterable[object], *, discovered_count: int = 0,
    processing_error_count: int = 0,
) -> None:
    """Persist a compact audit trail after a monitored scan has completed."""
    initialize_database(path)
    rows = list(rejected)
    title_count = sum(getattr(item, "stage", "") == "title" for item in rows)
    location_count = sum(getattr(item, "stage", "") == "location" for item in rows)
    with connect(path) as connection:
        for item in rows:
            connection.execute(
                """INSERT INTO collection_rejections
                (run_id, source_id, company, title, url, stage, reasons_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, item.source_id, item.company, item.title, item.url,
                    item.stage, json.dumps(item.reasons, ensure_ascii=False),
                ),
            )
        connection.execute(
            """UPDATE collection_runs SET discovered_count=?, rejected_title_count=?,
            rejected_location_count=?, processing_error_count=? WHERE run_id=?""",
            (
                discovered_count,
                title_count,
                location_count,
                processing_error_count,
                run_id,
            ),
        )


def get_collection_run(path: Path, run_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        run = connection.execute(
            "SELECT * FROM collection_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not run:
            return None
        sources = connection.execute(
            "SELECT * FROM collection_source_runs WHERE run_id=? ORDER BY company COLLATE NOCASE",
            (run_id,),
        ).fetchall()
        rejections = connection.execute(
            "SELECT * FROM collection_rejections WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    return {
        **dict(run),
        "sources": [dict(item) for item in sources],
        "rejections": [
            {**dict(item), "reasons": json.loads(item["reasons_json"])}
            for item in rejections
        ],
    }


def persist_cancelled_collection(
    path: Path, *, run_id: str, mode: str, started_at: str, sources_total: int,
    sources_checked: int, current_source: str | None,
) -> None:
    """Keep a cancelled scan auditable without applying a partial offer snapshot."""
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO collection_runs (
            run_id, mode, started_at, finished_at, status, sources_total, sources_ok,
            offers_saved, new_raw_versions, offers_marked_unavailable, error
            ) VALUES (?, ?, ?, ?, 'cancelled', ?, ?, 0, 0, 0, ?)""",
            (
                run_id, mode, started_at, datetime.now(UTC).isoformat(), sources_total,
                sources_checked, f"cancelled while checking {current_source or 'sources'}",
            ),
        )


def persist_collection(
    path: Path,
    offers: Iterable[CleanJob],
    source_urls: dict[str, str],
) -> tuple[int, int]:
    """Persist a collection atomically and return (offers_seen, new_raw_versions)."""
    initialize_database(path)
    seen = new_versions = 0
    with connect(path) as connection:
        for offer in offers:
            _, created = persist_clean_offer(
                connection,
                offer,
                source_url=source_urls[offer.source_id],
            )
            seen += 1
            new_versions += int(created)
    return seen, new_versions


def list_offers(
    path: Path,
    *,
    query: str = "",
    company: str = "",
    status: str = "",
    availability: str = "",
) -> list[dict]:
    initialize_database(path)
    clauses: list[str] = []
    params: list[str] = []
    if query:
        clauses.append("(title LIKE ? OR company LIKE ? OR offer_json LIKE ?)")
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern])
    if company:
        clauses.append("company = ?")
        params.append(company)
    if status:
        clauses.append("application_status = ?")
        params.append(status)
    if availability:
        clauses.append("availability_status = ?")
        params.append(availability)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(path) as connection:
        rows = connection.execute(
            f"""
            SELECT o.id, o.company, o.title, o.job_url, o.offer_json,
                   o.application_status, o.availability_status, o.first_seen_at,
                   o.last_seen_at, o.last_checked_at, o.disappeared_at,
                   o.last_collection_run_id, o.original_language,
                   o.language_confidence, o.language_detected_at,
                   o.current_content_sha256, o.evaluation_input_sha256,
                   o.needs_evaluation, o.assessment_json,
                   (
                       SELECT pr.profile_id
                       FROM evaluation_run_items AS eri
                       JOIN pipeline_runs AS pr ON pr.run_id = eri.run_id
                       WHERE eri.offer_id = o.id
                         AND eri.status = 'completed'
                         AND eri.final_assessment_json IS NOT NULL
                       ORDER BY pr.started_at DESC LIMIT 1
                   ) AS assessment_profile_id
            FROM offers AS o {where}
            ORDER BY last_seen_at DESC, company COLLATE NOCASE, title COLLATE NOCASE
            """,
            params,
        ).fetchall()
    return [
        {
            **dict(row),
            "offer": json.loads(row["offer_json"]),
            "assessment": (
                json.loads(row["assessment_json"]) if row["assessment_json"] else None
            ),
        }
        for row in rows
    ]


def get_offer(path: Path, offer_id: int) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT id, company, title, job_url, offer_json, application_status,
                   availability_status, first_seen_at, last_seen_at, last_checked_at,
                   disappeared_at, last_collection_run_id, original_language,
                   language_confidence, language_detected_at, current_content_sha256,
                   evaluation_input_sha256, needs_evaluation, assessment_json, judge_json
            FROM offers WHERE id = ?
            """,
            (offer_id,),
        ).fetchone()
    if not row:
        return None
    return {
        **dict(row),
        "offer": json.loads(row["offer_json"]),
        "assessment": json.loads(row["assessment_json"]) if row["assessment_json"] else None,
        "judge": json.loads(row["judge_json"]) if row["judge_json"] else None,
    }


def list_offer_section_evidence(path: Path, offer_id: int, content_sha256: str) -> list[dict]:
    """Evidence belongs to one content version and never leaks across an update."""
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """SELECT * FROM offer_section_evidence
            WHERE offer_id = ? AND content_sha256 = ?
            ORDER BY section_kind, category, created_at""",
            (offer_id, content_sha256),
        ).fetchall()
    return [{**dict(row), "value": json.loads(row["value_json"])} for row in rows]


def backfill_offer_section_evidence(path: Path) -> dict[str, int]:
    """Create deterministic evidence for current versions stored before migration 016.

    Historical offer versions are deliberately left untouched: evidence must always be
    tied to the exact content version it describes.
    """
    from .ats_scrapers import CleanJob

    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        rows = connection.execute(
            """SELECT id, offer_json, current_content_sha256 FROM offers
            WHERE current_content_sha256 IS NOT NULL"""
        ).fetchall()
        offers_processed = 0
        evidence_created = 0
        for row in rows:
            before = connection.execute(
                """SELECT COUNT(*) FROM offer_section_evidence
                WHERE offer_id = ? AND content_sha256 = ?""",
                (row["id"], row["current_content_sha256"]),
            ).fetchone()[0]
            _persist_offer_section_evidence(
                connection,
                offer_id=int(row["id"]),
                content_sha256=str(row["current_content_sha256"]),
                offer=CleanJob.model_validate(json.loads(row["offer_json"])),
                created_at=timestamp,
            )
            after = connection.execute(
                """SELECT COUNT(*) FROM offer_section_evidence
                WHERE offer_id = ? AND content_sha256 = ?""",
                (row["id"], row["current_content_sha256"]),
            ).fetchone()[0]
            offers_processed += 1
            evidence_created += int(after) - int(before)
    return {"offers_processed": offers_processed, "evidence_created": evidence_created}


def update_application_status(path: Path, offer_id: int, status: str) -> bool:
    initialize_database(path)
    with connect(path) as connection:
        cursor = connection.execute(
            "UPDATE offers SET application_status = ? WHERE id = ?", (status, offer_id)
        )
    return cursor.rowcount == 1


def get_collection_monitoring(path: Path) -> dict:
    """Return the compact operational state rendered by the Demo v2 panel."""
    initialize_database(path)
    with connect(path) as connection:
        latest = connection.execute(
            "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        counts = connection.execute(
            """
            SELECT
                count(*) AS total,
                sum(availability_status = 'active') AS active,
                sum(availability_status = 'unavailable') AS unavailable
            FROM offers
            """
        ).fetchone()
        sources = []
        if latest:
            sources = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT source_id, company, status, discovered_count, selected_count,
                           error, checked_at
                    FROM collection_source_runs
                    WHERE run_id = ?
                    ORDER BY company COLLATE NOCASE
                    """,
                    (latest["run_id"],),
                ).fetchall()
            ]
    return {
        "latest_run": dict(latest) if latest else None,
        "total_offers": int(counts["total"] or 0),
        "active_offers": int(counts["active"] or 0),
        "unavailable_offers": int(counts["unavailable"] or 0),
        "sources": sources,
    }


def save_user_profile(
    path: Path,
    *,
    profile_id: str,
    display_name: str,
    profile_json: dict,
    status: str,
) -> None:
    """Create or update a local profile draft without sharing its content externally."""
    if status not in {"draft", "cv_review", "cv_approved", "ready"}:
        raise ValueError("invalid profile status")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    version = max(1, int(profile_json.get("version", 1)))
    serialized = json.dumps(profile_json, ensure_ascii=False, sort_keys=True)
    completeness_status = {
        "draft": "profile_draft",
        "cv_review": "cv_review",
        "cv_approved": "cv_approved",
        "ready": "legacy_profile_only",
    }[status]
    with connect(path) as connection:
        existing_version = connection.execute(
            """
            SELECT profile_json FROM profile_versions
            WHERE profile_id = ? AND version = ?
            """,
            (profile_id, version),
        ).fetchone()
        if existing_version and json.loads(existing_version["profile_json"]) != profile_json:
            raise ValueError("profile version already exists with different content")
        connection.execute(
            """
            INSERT INTO user_profiles (
                profile_id, display_name, profile_json, status, created_at, updated_at,
                current_version, completeness_status, interview_language, canonical_language
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pl', 'en')
            ON CONFLICT(profile_id) DO UPDATE SET
                display_name=excluded.display_name,
                profile_json=excluded.profile_json,
                status=excluded.status,
                current_version=excluded.current_version,
                completeness_status=excluded.completeness_status,
                updated_at=excluded.updated_at
            """,
            (
                profile_id,
                display_name,
                serialized,
                status,
                timestamp,
                timestamp,
                version,
                completeness_status,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO profile_versions (
                profile_id, version, profile_json, status, source, created_at, approved_at
            ) VALUES (?, ?, ?, ?, 'profile_form', ?, ?)
            """,
            (
                profile_id,
                version,
                serialized,
                "approved" if status == "ready" else "proposed",
                timestamp,
                profile_json.get("approved_at"),
            ),
        )


def save_profile_document(
    path: Path,
    *,
    document_id: str,
    profile_id: str,
    original_filename: str,
    stored_path: Path,
    sha256: str,
    extraction_method: str,
    extracted_text: str,
    page_count: int,
    original_language: str = "mixed",
    extractor_version: str = "pdf-ocr-v1",
    corrected_text: str | None = None,
    review_status: str = "needs_review",
    page_text: list[str] | tuple[str, ...] = (),
) -> None:
    if original_language not in {"pl", "en", "mixed", "unknown"}:
        raise ValueError("invalid document language")
    if review_status not in {"needs_review", "approved", "rejected"}:
        raise ValueError("invalid document review status")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO profile_documents (
                document_id, profile_id, original_filename, stored_path, sha256, content_type,
                extraction_method, extracted_text, page_count, character_count, created_at,
                original_language, extractor_version, corrected_text, review_status, reviewed_at,
                page_text_json
            ) VALUES (?, ?, ?, ?, ?, 'application/pdf', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                profile_id,
                original_filename,
                str(stored_path),
                sha256,
                extraction_method,
                extracted_text,
                page_count,
                len(extracted_text),
                timestamp,
                original_language,
                extractor_version,
                corrected_text,
                review_status,
                timestamp if review_status == "approved" else None,
                json.dumps(list(page_text), ensure_ascii=False),
            ),
        )
        connection.execute(
            """
            UPDATE user_profiles
            SET status = CASE WHEN status = 'ready' THEN 'cv_review' ELSE status END,
                completeness_status = CASE
                    WHEN status = 'draft' THEN 'profile_draft'
                    ELSE 'cv_review'
                END,
                updated_at = ?
            WHERE profile_id = ?
            """,
            (timestamp, profile_id),
        )


def approve_profile_document(
    path: Path,
    *,
    profile_id: str,
    document_id: str,
    corrected_text: str,
    original_language: str,
) -> int:
    """Approve the reviewed CV text and create an immutable profile version."""
    reviewed_text = corrected_text.strip()
    if not reviewed_text:
        raise ValueError("corrected CV text cannot be empty")
    if original_language not in {"pl", "en", "mixed", "unknown"}:
        raise ValueError("invalid document language")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        profile = connection.execute(
            "SELECT * FROM user_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        document = connection.execute(
            """
            SELECT document_id FROM profile_documents
            WHERE document_id = ? AND profile_id = ?
            """,
            (document_id, profile_id),
        ).fetchone()
        if not profile or not document:
            raise ValueError("profile document not found")
        payload = json.loads(profile["profile_json"])
        has_scoring_profile = bool(
            payload.get("target_roles")
            and payload.get("evidence")
            and str(payload.get("location_rule") or "").strip()
        )
        next_version = int(profile["current_version"]) + 1
        payload["version"] = next_version
        payload["approved_at"] = timestamp if has_scoring_profile else None
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        version_status = "approved" if has_scoring_profile else "cv_approved"
        profile_status = "ready" if has_scoring_profile else "cv_approved"
        completeness_status = (
            "ready_for_scoring" if has_scoring_profile else "cv_approved"
        )
        connection.execute(
            """
            UPDATE profile_documents
            SET corrected_text = ?, original_language = ?, review_status = 'approved',
                reviewed_at = ?
            WHERE document_id = ?
            """,
            (reviewed_text, original_language, timestamp, document_id),
        )
        connection.execute(
            """
            UPDATE profile_versions
            SET superseded_at = ?
            WHERE profile_id = ? AND version = ? AND superseded_at IS NULL
            """,
            (timestamp, profile_id, profile["current_version"]),
        )
        connection.execute(
            """
            INSERT INTO profile_versions (
                profile_id, version, profile_json, status, source, created_at, approved_at
            ) VALUES (?, ?, ?, ?, 'cv_review', ?, ?)
            """,
            (
                profile_id,
                next_version,
                serialized,
                version_status,
                timestamp,
                timestamp if has_scoring_profile else None,
            ),
        )
        connection.execute(
            """
            UPDATE user_profiles
            SET profile_json = ?, status = ?, current_version = ?,
                completeness_status = ?, updated_at = ?
            WHERE profile_id = ?
            """,
            (
                serialized,
                profile_status,
                next_version,
                completeness_status,
                timestamp,
                profile_id,
            ),
        )
    return next_version


def list_profile_versions(path: Path, profile_id: str) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM profile_versions
            WHERE profile_id = ? ORDER BY version DESC
            """,
            (profile_id,),
        ).fetchall()
    return [{**dict(row), "profile": json.loads(row["profile_json"])} for row in rows]


def list_user_profiles(path: Path) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT p.*, d.original_filename, d.extraction_method, d.page_count,
                   d.character_count, d.created_at AS document_created_at
            FROM user_profiles p
            LEFT JOIN profile_documents d ON d.document_id = (
                SELECT document_id FROM profile_documents
                WHERE profile_id = p.profile_id ORDER BY created_at DESC LIMIT 1
            )
            ORDER BY p.updated_at DESC
            """
        ).fetchall()
    return [{**dict(row), "profile": json.loads(row["profile_json"])} for row in rows]


def get_user_profile(path: Path, profile_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM user_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        document = connection.execute(
            """
            SELECT * FROM profile_documents WHERE profile_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (profile_id,),
        ).fetchone()
    if not row:
        return None
    result = {**dict(row), "profile": json.loads(row["profile_json"])}
    if document:
        document_result = dict(document)
        document_result["pages"] = json.loads(document["page_text_json"])
        result["document"] = document_result
    else:
        result["document"] = None
    return result


PROFILE_READINESS_REQUIREMENTS = {
    "target_role": "Dodaj i zatwierdź co najmniej jedną docelową rolę.",
    "work_location": "Określ lokalizację, z której możesz pracować.",
    "work_model": "Określ dopuszczalny model pracy (zdalnie, hybrydowo lub stacjonarnie).",
    "contract": "Określ warunki umowy — także gdy nie masz ograniczenia.",
    "language": "Określ wymagania językowe — także gdy nie masz ograniczenia.",
    "travel": "Określ gotowość do podróży — także gdy nie masz ograniczenia.",
}
_SINGLE_VALUE_FACT_CATEGORIES = frozenset(set(PROFILE_READINESS_REQUIREMENTS) - {"target_role"})


def list_profile_facts(path: Path, profile_id: str) -> list[dict]:
    """Return auditable facts; values are decoded only at the storage boundary."""
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """SELECT * FROM profile_facts WHERE profile_id = ?
            ORDER BY status = 'approved' DESC, category, approved_at DESC, created_at DESC""",
            (profile_id,),
        ).fetchall()
    return [{**dict(row), "value": json.loads(row["value_json"])} for row in rows]


def list_profile_fact_conflicts(path: Path, profile_id: str) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """SELECT c.*, a.value_json AS value_a_json, b.value_json AS value_b_json
            FROM profile_fact_conflicts c
            JOIN profile_facts a ON a.fact_id = c.fact_id_a
            JOIN profile_facts b ON b.fact_id = c.fact_id_b
            WHERE c.profile_id = ? ORDER BY c.status, c.created_at DESC""",
            (profile_id,),
        ).fetchall()
    return [
        {
            **dict(row),
            "value_a": json.loads(row["value_a_json"]),
            "value_b": json.loads(row["value_b_json"]),
        }
        for row in rows
    ]


def _fact_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fact_has_explicit_value(value: object) -> bool:
    """Distinguish an explicit answer (including ``False``) from an empty draft."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_fact_has_explicit_value(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and any(_fact_has_explicit_value(item) for item in value)
    return True


def _readiness_from_facts(facts: list[dict], conflicts: list[dict]) -> dict:
    """The sole deterministic definition of a profile eligible for scoring."""
    approved = [fact for fact in facts if fact["status"] == "approved"]
    usable = [fact for fact in approved if fact["usable_for_scoring"]]
    usable_categories = {fact["category"] for fact in usable}
    missing = [
        message
        for category, message in PROFILE_READINESS_REQUIREMENTS.items()
        if category not in usable_categories
    ]
    if not usable_categories.intersection({"experience", "achievement"}):
        missing.append("Dodaj zatwierdzone doświadczenie lub osiągnięcie używane przez scoring.")
    required_unresolved = [
        fact
        for fact in usable
        if fact["priority"] == "required" and not _fact_has_explicit_value(fact["value"])
    ]
    open_conflicts = [item for item in conflicts if item["status"] == "open"]
    return {
        "ready_for_scoring": not missing and not required_unresolved and not open_conflicts,
        "approved_facts": approved,
        "usable_for_scoring": usable,
        "draft_facts": [fact for fact in facts if fact["status"] == "draft"],
        "missing": missing,
        "required_unresolved": required_unresolved,
        "conflicts": open_conflicts,
    }


def _refresh_fact_conflicts(connection: sqlite3.Connection, profile_id: str, category: str) -> None:
    if category not in _SINGLE_VALUE_FACT_CATEGORIES:
        return
    facts = connection.execute(
        """SELECT fact_id, value_json FROM profile_facts
        WHERE profile_id = ? AND category = ? AND status = 'approved'""",
        (profile_id, category),
    ).fetchall()
    if len({_fact_text(json.loads(row["value_json"])) for row in facts}) < 2:
        return
    timestamp = datetime.now(UTC).isoformat()
    for index, left in enumerate(facts):
        for right in facts[index + 1 :]:
            if left["value_json"] == right["value_json"]:
                continue
            fact_id_a, fact_id_b = sorted((left["fact_id"], right["fact_id"]))
            connection.execute(
                """INSERT OR IGNORE INTO profile_fact_conflicts (
                conflict_id, profile_id, category, fact_id_a, fact_id_b, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f"conflict-{uuid.uuid4().hex}",
                    profile_id,
                    category,
                    fact_id_a,
                    fact_id_b,
                    timestamp,
                ),
            )


def save_profile_fact(
    path: Path,
    *,
    profile_id: str,
    category: str,
    value: dict | str | list,
    source_type: str,
    source_ref: str,
    source_quote: str,
    priority: str | None = None,
    usable_for_scoring: bool = False,
    usable_for_cv: bool = False,
    status: str = "draft",
    fact_id: str | None = None,
) -> str:
    """Persist one explicit fact. Drafts never enter scoring before approval."""
    if not category.strip() or not source_ref.strip() or not source_quote.strip():
        raise ValueError("category, source reference and source quote are required")
    if source_type not in {"cv", "user_message", "career_base"}:
        raise ValueError("invalid fact source type")
    if priority not in {None, "preference", "important", "required"}:
        raise ValueError("invalid fact priority")
    if status != "draft":
        raise ValueError("new profile facts must be saved as drafts and reviewed explicitly")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    fact_id = fact_id or f"fact-{uuid.uuid4().hex}"
    with connect(path) as connection:
        profile = connection.execute(
            "SELECT current_version FROM user_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if not profile:
            raise ValueError("profile not found")
        connection.execute(
            """INSERT INTO profile_facts (
            fact_id, profile_id, category, value_json, status, source_type, source_ref,
            source_quote, priority, usable_for_scoring, usable_for_cv, profile_version,
            created_at, approved_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_id,
                profile_id,
                category.strip(),
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                status,
                source_type,
                source_ref.strip(),
                source_quote.strip(),
                priority,
                int(usable_for_scoring),
                int(usable_for_cv),
                int(profile["current_version"]) if status == "approved" else None,
                timestamp,
                timestamp if status == "approved" else None,
                timestamp,
            ),
        )
    return fact_id


def profile_readiness(path: Path, profile_id: str) -> dict:
    """Deterministic gate: only approved facts with provenance can unlock scoring."""
    profile = get_user_profile(path, profile_id)
    if not profile:
        raise ValueError("profile not found")
    facts = list_profile_facts(path, profile_id)
    return _readiness_from_facts(facts, list_profile_fact_conflicts(path, profile_id))


def is_profile_ready_for_scoring(path: Path, profile_id: str) -> bool:
    """Use this guard at every scoring or offer-advice entry point."""
    return bool(get_user_profile(path, profile_id)) and profile_readiness(path, profile_id)[
        "ready_for_scoring"
    ]


def _store_profile_fact_snapshot(connection: sqlite3.Connection, profile_id: str) -> int:
    """Version the approved fact set and mirror the one readiness predicate in the profile."""
    timestamp = datetime.now(UTC).isoformat()
    profile = connection.execute(
        "SELECT * FROM user_profiles WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    if not profile:
        raise ValueError("profile not found")
    fact_rows = connection.execute(
        "SELECT * FROM profile_facts WHERE profile_id = ?", (profile_id,)
    ).fetchall()
    facts = [{**dict(row), "value": json.loads(row["value_json"])} for row in fact_rows]
    conflict_rows = connection.execute(
        "SELECT status FROM profile_fact_conflicts WHERE profile_id = ?", (profile_id,)
    ).fetchall()
    readiness = _readiness_from_facts(facts, [dict(row) for row in conflict_rows])
    next_version = int(profile["current_version"]) + 1
    payload = json.loads(profile["profile_json"])
    payload.update(
        {
            "version": next_version,
            "approved_fact_ids": [fact["fact_id"] for fact in readiness["approved_facts"]],
            "approved_at": timestamp if readiness["ready_for_scoring"] else None,
        }
    )
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    connection.execute(
        """UPDATE profile_versions SET superseded_at = ?
        WHERE profile_id = ? AND version = ? AND superseded_at IS NULL""",
        (timestamp, profile_id, profile["current_version"]),
    )
    connection.execute(
        """INSERT INTO profile_versions (
        profile_id, version, profile_json, status, source, created_at, approved_at)
        VALUES (?, ?, ?, ?, 'fact_review', ?, ?)""",
        (
            profile_id,
            next_version,
            serialized,
            "approved" if readiness["ready_for_scoring"] else "proposed",
            timestamp,
            timestamp if readiness["ready_for_scoring"] else None,
        ),
    )
    connection.execute(
        """UPDATE user_profiles SET profile_json = ?, status = ?, current_version = ?,
        completeness_status = ?, updated_at = ? WHERE profile_id = ?""",
        (
            serialized,
            "ready" if readiness["ready_for_scoring"] else "cv_approved",
            next_version,
            "ready_for_scoring" if readiness["ready_for_scoring"] else "profile_facts_incomplete",
            timestamp,
            profile_id,
        ),
    )
    return next_version


def review_profile_fact(path: Path, *, fact_id: str, approve: bool) -> None:
    """Approve or reject a draft, preserving an immutable profile-version snapshot."""
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fact = connection.execute(
            "SELECT * FROM profile_facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if not fact or fact["status"] != "draft":
            raise ValueError("only a draft fact can be reviewed")
        if not approve:
            connection.execute(
                "UPDATE profile_facts SET status = 'rejected', updated_at = ? WHERE fact_id = ?",
                (timestamp, fact_id),
            )
            return
        connection.execute(
            """UPDATE profile_facts SET status = 'approved', approved_at = ?,
            updated_at = ? WHERE fact_id = ?""",
            (timestamp, timestamp, fact_id),
        )
        _refresh_fact_conflicts(connection, fact["profile_id"], fact["category"])
        profile_version = _store_profile_fact_snapshot(connection, fact["profile_id"])
        connection.execute(
            "UPDATE profile_facts SET profile_version = ? WHERE fact_id = ?",
            (profile_version, fact_id),
        )


def resolve_profile_fact_conflict(
    path: Path, *, conflict_id: str, winner_fact_id: str, resolution_note: str
) -> None:
    if not resolution_note.strip():
        raise ValueError("a conflict resolution note is required")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        conflict = connection.execute(
            "SELECT * FROM profile_fact_conflicts WHERE conflict_id = ? AND status = 'open'",
            (conflict_id,),
        ).fetchone()
        if not conflict:
            raise ValueError("open conflict not found")
        candidates = {conflict["fact_id_a"], conflict["fact_id_b"]}
        if winner_fact_id not in candidates:
            raise ValueError("selected winning fact is not part of this conflict")
        loser_fact_id = next(fact_id for fact_id in candidates if fact_id != winner_fact_id)
        connection.execute(
            "UPDATE profile_facts SET status = 'superseded', updated_at = ? WHERE fact_id = ?",
            (timestamp, loser_fact_id),
        )
        connection.execute(
            """UPDATE profile_fact_conflicts SET status = 'resolved',
            resolution_note = ?, resolved_at = ?
            WHERE profile_id = ? AND status = 'open'
              AND (fact_id_a = ? OR fact_id_b = ?)""",
            (
                resolution_note.strip(),
                timestamp,
                conflict["profile_id"],
                loser_fact_id,
                loser_fact_id,
            ),
        )
        _store_profile_fact_snapshot(connection, conflict["profile_id"])


def save_lab_model_profile(
    path: Path,
    *,
    model_profile_id: str,
    display_name: str,
    role: str,
    model_path: str,
    config_json: dict,
) -> None:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO lab_model_profiles (
                model_profile_id, display_name, role, model_path, config_json, created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_profile_id) DO UPDATE SET
                display_name=excluded.display_name, role=excluded.role,
                model_path=excluded.model_path,
                config_json=excluded.config_json, updated_at=excluded.updated_at
            """,
            (
                model_profile_id,
                display_name,
                role,
                model_path,
                json.dumps(config_json),
                timestamp,
                timestamp,
            ),
        )


def list_lab_model_profiles(path: Path) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM lab_model_profiles ORDER BY updated_at DESC"
        ).fetchall()
    return [{**dict(row), "config": json.loads(row["config_json"])} for row in rows]


def save_lab_experiment(
    path: Path,
    *,
    experiment_id: str,
    display_name: str,
    profile_id: str | None,
    dataset_key: str,
    task_kind: str,
    model_profile_id: str | None,
    config_json: dict,
) -> None:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO lab_experiments (
                experiment_id, display_name, profile_id, dataset_key, task_kind, model_profile_id,
                config_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
            """,
            (
                experiment_id,
                display_name,
                profile_id,
                dataset_key,
                task_kind,
                model_profile_id,
                json.dumps(config_json),
                timestamp,
                timestamp,
            ),
        )


def list_lab_experiments(path: Path) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT e.*, p.display_name AS profile_name, m.display_name AS model_name
            FROM lab_experiments e
            LEFT JOIN user_profiles p ON p.profile_id = e.profile_id
            LEFT JOIN lab_model_profiles m ON m.model_profile_id = e.model_profile_id
            ORDER BY e.created_at DESC
            """
        ).fetchall()
    return [{**dict(row), "config": json.loads(row["config_json"])} for row in rows]


def get_latest_evaluation_run(path: Path) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["profile"] = json.loads(row["profile_json"]) if row["profile_json"] else None
    result["models"] = json.loads(row["models_json"])
    result["prompt_versions"] = json.loads(row["prompt_versions_json"])
    return result


def list_evaluation_run_items(path: Path, run_id: str) -> list[dict]:
    initialize_database(path)
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT eri.*, o.company, o.title, o.job_url
            FROM evaluation_run_items eri
            JOIN offers o ON o.id = eri.offer_id
            WHERE eri.run_id = ?
            ORDER BY json_extract(eri.final_assessment_json, '$.final_score') DESC,
                     o.company COLLATE NOCASE
            """,
            (run_id,),
        ).fetchall()
    json_fields = {
        "input_snapshot_json": "input_snapshot",
        "extracted_json": "extracted",
        "draft_json": "draft",
        "judgment_json": "judgment",
        "final_assessment_json": "final_assessment",
        "timings_json": "timings",
        "retry_counts_json": "retry_counts",
    }
    results = []
    for row in rows:
        item = dict(row)
        for source, target in json_fields.items():
            item[target] = json.loads(row[source]) if row[source] else None
        results.append(item)
    return results


def create_evaluation_run(
    path: Path,
    run: PipelineRun,
    profile: CandidateProfile,
    items: Iterable[EvaluationRunItem],
) -> None:
    """Atomically persist an approved profile snapshot and all pending run items."""
    if not profile.approved:
        raise ValueError("candidate profile must be approved before starting a demo run")
    item_list = list(items)
    if run.profile_id != profile.profile_id or run.profile_version != profile.version:
        raise ValueError("pipeline run does not reference the supplied profile version")
    if run.total_items != len(item_list):
        raise ValueError("pipeline run total does not match its evaluation items")
    initialize_database(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT run_id FROM pipeline_runs WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if active:
            raise ValueError(f"another evaluation run is already active: {active['run_id']}")
        connection.execute(
            """
            INSERT INTO pipeline_runs (
                run_id, source_id, run_type, started_at, finished_at, status,
                total_items, completed_items, current_stage, profile_id, profile_version,
                profile_json, prompt_versions_json, mlflow_run_id, models_json,
                parent_run_id, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.source_id,
                run.run_type,
                run.started_at.isoformat(),
                run.finished_at.isoformat() if run.finished_at else None,
                run.status.value,
                run.total_items,
                run.completed_items,
                run.current_stage,
                run.profile_id,
                run.profile_version,
                profile.model_dump_json(),
                json.dumps(run.prompt_versions, sort_keys=True),
                run.mlflow_run_id,
                json.dumps(run.model_configurations, sort_keys=True),
                run.parent_run_id,
                run.error,
            ),
        )
        for item in item_list:
            if item.run_id != run.run_id:
                raise ValueError("evaluation item belongs to a different run")
            connection.execute(
                """
                INSERT INTO evaluation_run_items (
                    item_id, run_id, offer_id, status, input_sha256,
                    input_snapshot_json, timings_json, retry_counts_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.run_id,
                    item.offer_id,
                    item.status.value,
                    item.input_sha256,
                    json.dumps(item.input_snapshot, sort_keys=True),
                    json.dumps(item.timings_ms, sort_keys=True),
                    json.dumps(item.retry_counts, sort_keys=True),
                    item.error,
                ),
            )


def retry_failed_evaluation_run(path: Path, source_run_id: str) -> str:
    """Create a new, auditable run containing only failed items from a prior run."""
    from .domain import CandidateProfile, EvaluationRunItem, EvaluationRunStatus, PipelineRun

    source = get_pipeline_run(path, source_run_id)
    if not source:
        raise ValueError("source evaluation run does not exist")
    if source["status"] == EvaluationRunStatus.RUNNING.value:
        raise ValueError("cannot retry a running evaluation run")
    failed_items = [
        item
        for item in list_evaluation_run_items(path, source_run_id)
        if item["status"] == "failed"
    ]
    if not failed_items:
        raise ValueError("source evaluation run has no failed items")

    profile_data = source.get("profile")
    if not profile_data:
        raise ValueError("source evaluation run has no profile snapshot")
    profile = CandidateProfile.model_validate(profile_data)
    started_at = datetime.now(UTC)
    run_id = "demo-retry-" + sha256(
        f"{source_run_id}:{started_at.isoformat()}".encode()
    ).hexdigest()[:16]
    items = [
        EvaluationRunItem(
            item_id=f"{run_id}:{item['offer_id']}",
            run_id=run_id,
            offer_id=item["offer_id"],
            input_sha256=item["input_sha256"],
            input_snapshot=item["input_snapshot"],
        )
        for item in failed_items
    ]
    run = PipelineRun(
        run_id=run_id,
        source_id=source["source_id"],
        run_type=source["run_type"],
        started_at=started_at,
        status=EvaluationRunStatus.RUNNING,
        total_items=len(items),
        current_stage="retrying failed items",
        profile_id=profile.profile_id,
        profile_version=profile.version,
        prompt_versions=source["prompt_versions"],
        model_configurations=source["models"],
        parent_run_id=source_run_id,
    )
    create_evaluation_run(path, run, profile, items)
    return run_id


def update_evaluation_run_item(
    path: Path,
    item: EvaluationRunItem,
    *,
    is_completed: bool = False,
    current_stage_label: str | None = None,
) -> None:
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            """
            UPDATE evaluation_run_items SET
                status = ?, extracted_json = ?, draft_json = ?, judgment_json = ?,
                final_assessment_json = ?, timings_json = ?, retry_counts_json = ?, error = ?
            WHERE item_id = ?
            """,
            (
                item.status.value,
                item.extracted.model_dump_json() if item.extracted else None,
                item.draft.model_dump_json() if item.draft else None,
                item.judgment.model_dump_json() if item.judgment else None,
                item.final_assessment.model_dump_json() if item.final_assessment else None,
                json.dumps(item.timings_ms, sort_keys=True),
                json.dumps(item.retry_counts, sort_keys=True),
                item.error,
                item.item_id,
            ),
        )
        if item.final_assessment:
            connection.execute(
                """
                UPDATE offers
                SET assessment_json = ?, judge_json = ?,
                    evaluation_input_sha256 = current_content_sha256,
                    needs_evaluation = 0
                WHERE id = ?
                """,
                (
                    item.final_assessment.model_dump_json(),
                    item.judgment.model_dump_json() if item.judgment else None,
                    item.offer_id,
                ),
            )
        if is_completed:
            _recalculate_completed_items(connection, item.run_id)
        if current_stage_label:
            connection.execute(
                "UPDATE pipeline_runs SET current_stage = ? WHERE run_id = ?",
                (current_stage_label, item.run_id),
            )


def update_run_state(
    path: Path,
    run_id: str,
    status: str | None = None,
    current_stage: str | None = None,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> None:
    initialize_database(path)
    updates = []
    params = []
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if current_stage is not None:
        updates.append("current_stage = ?")
        params.append(current_stage)
    if error is not None:
        updates.append("error = ?")
        params.append(error)
    if finished_at is not None:
        updates.append("finished_at = ?")
        params.append(finished_at.isoformat())

    if not updates:
        return

    params.append(run_id)
    with connect(path) as connection:
        connection.execute(
            f"UPDATE pipeline_runs SET {', '.join(updates)} WHERE run_id = ?", params
        )


def get_pipeline_run(path: Path, run_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["profile"] = json.loads(row["profile_json"]) if row["profile_json"] else None
    result["models"] = json.loads(row["models_json"])
    result["prompt_versions"] = json.loads(row["prompt_versions_json"])
    return result


def list_pipeline_runs(path: Path, *, status: str = "") -> list[dict]:
    initialize_database(path)
    where = "WHERE status = ?" if status else ""
    params = [status] if status else []
    with connect(path) as connection:
        rows = connection.execute(
            f"""
            SELECT pipeline_runs.*,
                   (SELECT count(*) FROM evaluation_run_items
                    WHERE evaluation_run_items.run_id = pipeline_runs.run_id
                      AND status = 'failed') AS failed_items
            FROM pipeline_runs {where} ORDER BY started_at DESC
            """,
            params,
        ).fetchall()

    results = []
    for row in rows:
        result = dict(row)
        result["profile"] = json.loads(row["profile_json"]) if row["profile_json"] else None
        result["models"] = json.loads(row["models_json"])
        result["prompt_versions"] = json.loads(row["prompt_versions_json"])
        results.append(result)
    return results


def _recalculate_completed_items(connection: sqlite3.Connection, run_id: str) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET completed_items = (
            SELECT count(*)
            FROM evaluation_run_items
            WHERE run_id = pipeline_runs.run_id AND status IN ('completed', 'failed')
        )
        WHERE run_id = ?
        """,
        (run_id,),
    )


def mark_interrupted_runs(path: Path) -> None:
    initialize_database(path)
    interrupted_at = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        connection.execute(
            """
            UPDATE evaluation_run_items
            SET status = 'interrupted'
            WHERE status = 'running'
              AND run_id IN (SELECT run_id FROM pipeline_runs WHERE status = 'running')
            """,
        )
        connection.execute(
            """
            UPDATE pipeline_runs
            SET status = 'interrupted', current_stage = 'interrupted', finished_at = ?
            WHERE status = 'running'
            """,
            (interrupted_at,),
        )


def cancel_run(path: Path, run_id: str) -> bool:
    initialize_database(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            "SELECT status FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not run or run["status"] not in {"pending", "running"}:
            return False
        connection.execute(
            """
            UPDATE evaluation_run_items
            SET status = 'cancelled'
            WHERE run_id = ? AND status IN ('pending', 'running')
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE pipeline_runs
            SET status = 'cancelled', current_stage = 'cancelled', finished_at = ?
            WHERE run_id = ? AND status IN ('pending', 'running')
            """,
            (datetime.now(UTC).isoformat(), run_id),
        )
        _recalculate_completed_items(connection, run_id)
    return True


def resume_run(path: Path, run_id: str) -> None:
    initialize_database(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT run_id FROM pipeline_runs WHERE status = 'running' AND run_id != ? LIMIT 1",
            (run_id,),
        ).fetchone()
        if active:
            raise ValueError(f"another evaluation run is already active: {active['run_id']}")
        run = connection.execute(
            "SELECT status FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not run:
            raise ValueError(f"evaluation run does not exist: {run_id}")
        if run["status"] not in {"interrupted", "cancelled", "partial"}:
            raise ValueError(f"evaluation run cannot be resumed from status: {run['status']}")
        connection.execute(
            """
            UPDATE evaluation_run_items
            SET status = 'pending', draft_json = NULL, judgment_json = NULL,
                final_assessment_json = NULL, timings_json = '{}', retry_counts_json = '{}',
                error = NULL
            WHERE run_id = ? AND status IN ('interrupted', 'cancelled', 'running')
            """,
            (run_id,),
        )
        _recalculate_completed_items(connection, run_id)
        connection.execute(
            """
            UPDATE pipeline_runs
            SET status = 'running', current_stage = 'starting', finished_at = NULL, error = NULL
            WHERE run_id = ?
            """,
            (run_id,),
        )
