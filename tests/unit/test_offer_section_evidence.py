from job_scout.ats_scrapers import CleanJob
from job_scout.storage import (
    backfill_offer_section_evidence,
    connect,
    get_offer,
    initialize_database,
    list_offer_section_evidence,
    persist_clean_offer,
)


def _offer(description: str) -> CleanJob:
    return CleanJob(
        source_id="evidence-source",
        company="Evidence Co",
        external_id="evidence-1",
        title="AI Engineer",
        url="https://example.com/jobs/evidence-1",
        locations=["Poland"],
        description=description,
        analysis_text=description,
        raw_sha256="e" * 64,
        extraction_method="test",
    )


def test_persisted_offer_sections_are_quoted_and_bound_to_content_version(tmp_path):
    path = tmp_path / "evidence.db"
    initialize_database(path)
    description = """
    Responsibilities: Build local AI tools and evaluate their output.
    Requirements: Python experience and production ML delivery.
    We offer fully remote work and 15 000 - 18 000 PLN.
    """
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, _offer(description), source_url="https://example.com/careers"
        )

    stored = get_offer(path, offer_id)
    evidence = list_offer_section_evidence(path, offer_id, stored["current_content_sha256"])
    kinds = {item["section_kind"] for item in evidence}
    assert {"responsibilities", "requirements", "work_conditions", "compensation"} <= kinds
    normalized_source = " ".join(description.split())
    assert all(" ".join(item["source_quote"].split()) in normalized_source for item in evidence)
    assert all(item["content_sha256"] == stored["current_content_sha256"] for item in evidence)


def test_missing_labelled_sections_do_not_create_generated_evidence(tmp_path):
    path = tmp_path / "evidence.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection,
            _offer("A short company introduction without labelled role sections."),
            source_url="https://example.com/careers",
        )

    stored = get_offer(path, offer_id)
    evidence = list_offer_section_evidence(path, offer_id, stored["current_content_sha256"])
    assert evidence == []


def test_backfill_is_idempotent_for_existing_offer_versions(tmp_path):
    path = tmp_path / "evidence.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection,
            _offer("Responsibilities: Build tools. Requirements: Python experience."),
            source_url="https://example.com/careers",
        )
        connection.execute("DELETE FROM offer_section_evidence WHERE offer_id = ?", (offer_id,))

    first = backfill_offer_section_evidence(path)
    second = backfill_offer_section_evidence(path)
    stored = get_offer(path, offer_id)
    evidence = list_offer_section_evidence(path, offer_id, stored["current_content_sha256"])
    assert first == {"offers_processed": 1, "evidence_created": 2}
    assert second == {"offers_processed": 1, "evidence_created": 0}
    assert {item["section_kind"] for item in evidence} == {"responsibilities", "requirements"}
