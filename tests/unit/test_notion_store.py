import json

import httpx

from job_scout.ats_scrapers import CleanJob, SupplementalInfo
from job_scout.notion_store import NotionOffersStore


def offer(**overrides):
    values = {
        "source_id": "demo",
        "company": "Demo AI",
        "external_id": "test-1",
        "title": "AI Engineer",
        "url": "https://example.com/jobs/test-1",
        "locations": ["Remote from Poland"],
        "description": "Build AI products.",
        "analysis_text": "Build AI products remotely with Python.",
        "raw_sha256": "a" * 64,
        "extraction_method": "fixture",
    }
    values.update(overrides)
    return CleanJob(**values)


def schema_properties():
    types = {
        "Stanowisko": "title",
        "Firma": "rich_text",
        "Źródło": "rich_text",
        "URL": "url",
        "Offer_key": "rich_text",
        "Content_hash": "rich_text",
        "Data": "date",
        "Tryb_pracy": "select",
        "Umowa": "select",
        "Status_Scrapera": "select",
        "Pipeline_status": "select",
        "Alert_status": "select",
        "Alert_key": "rich_text",
        "Kluczowe_wymagania": "rich_text",
        "Ocena_AI": "number",
        "Decyzja_AI": "select",
        "Uzasadnienie": "rich_text",
    }
    properties = {name: {"type": property_type} for name, property_type in types.items()}
    properties["Pipeline_status"]["select"] = {
        "options": [{"name": name} for name in ("Processing", "Completed", "Failed")]
    }
    properties["Alert_status"]["select"] = {
        "options": [{"name": name} for name in ("Pending", "Sent", "Failed", "Not eligible")]
    }
    return properties


def test_upsert_creates_processing_page_with_content_hash():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/databases/database":
            return httpx.Response(200, json={"data_sources": [{"id": "source"}]})
        if request.url.path == "/v1/data_sources/source":
            return httpx.Response(200, json={"properties": schema_properties()})
        if request.url.path == "/v1/data_sources/source/query":
            return httpx.Response(200, json={"results": []})
        if request.url.path == "/v1/pages" and request.method == "POST":
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    store = NotionOffersStore("secret", "database", client=client)
    content_hash = store.content_hash(offer())
    page = store.upsert_offer(offer(), content_hash=content_hash)

    assert page.page_id == "page-1"
    assert page.created is True
    payload = json.loads(requests[-1].content)
    assert payload["parent"] == {"type": "data_source_id", "data_source_id": "source"}
    assert payload["properties"]["Offer_key"]["rich_text"][0]["text"]["content"] == "demo:test-1"
    assert payload["properties"]["Content_hash"]["rich_text"][0]["text"]["content"] == content_hash
    assert payload["properties"]["Pipeline_status"]["select"]["name"] == "Processing"


def test_content_hash_changes_when_model_relevant_supplemental_fact_changes():
    first = offer()
    changed = offer(
        supplemental_info=SupplementalInfo.model_validate(
            {"work_conditions": [{"category": "hybrid", "evidence": "Three office days in Warsaw"}]}
        )
    )
    assert NotionOffersStore.content_hash(first) != NotionOffersStore.content_hash(changed)


def test_current_page_is_skipped_only_after_completed_matching_hash():
    job = offer()
    content_hash = NotionOffersStore.content_hash(job)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/database":
            return httpx.Response(200, json={"data_sources": [{"id": "source"}]})
        if request.url.path == "/v1/data_sources/source":
            return httpx.Response(200, json={"properties": schema_properties()})
        if request.url.path == "/v1/data_sources/source/query":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "page-1",
                            "properties": {
                                "Content_hash": {"rich_text": [{"plain_text": content_hash}]},
                                "Pipeline_status": {"select": {"name": "Completed"}},
                                "Alert_key": {"rich_text": []},
                                "Alert_status": {"select": {"name": "Not eligible"}},
                            },
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store = NotionOffersStore(
        "secret", "database", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    page = store.lookup_offer(job)
    assert store.is_current(page, content_hash)
