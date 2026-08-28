import json

import httpx
import pytest

from job_scout.ats_scrapers import (
    DiscoveredJob,
    HttpClient,
    LeverAdapter,
    clean_job,
    extract_job_links,
    html_to_text,
)
from job_scout.sources import SourceConfig


def source():
    return SourceConfig(
        id="example",
        company="Example",
        adapter="html",
        career_url="https://example.com/careers",
    )


def test_extracts_json_ld_job_from_official_page():
    record = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Applied AI Engineer",
        "url": "https://example.com/jobs/123",
        "description": "<p>Build local AI systems.</p>",
    }
    html = f'<script type="application/ld+json">{json.dumps(record)}</script>'
    jobs = extract_job_links(html, "https://example.com/careers", source())
    assert len(jobs) == 1
    assert jobs[0].title == "Applied AI Engineer"


def test_rejects_external_job_board_link():
    html = '<a href="https://example-job-board.invalid/jobs/1">AI Engineer</a>'
    assert extract_job_links(html, "https://example.com/careers", source()) == []


@pytest.mark.parametrize(
    ("career_url", "job_url", "title"),
    [
        (
            "https://www.coi.gov.pl/oferty-pracy/programowanie",
            "/oferty-pracy/oferta/programowanie-warszawa-hybrydowa-2055",
            "Programista / Programistka Java + Angular",
        ),
        (
            "https://bip2.opi.org.pl/opi/oferty-pracy/2026",
            "/opi/oferty-pracy/2026/21307,AnalitykProjektant.html",
            "Analityk/Projektant - starszy specjalista",
        ),
    ],
)
def test_extracts_jobs_from_polish_public_sector_career_paths(career_url, job_url, title):
    public_source = SourceConfig(
        id="public",
        company="Public IT",
        adapter="html",
        career_url=career_url,
    )
    jobs = extract_job_links(f'<a href="{job_url}">{title}</a>', career_url, public_source)

    assert len(jobs) == 1
    assert jobs[0].title == title


def test_rejects_service_page_that_only_contains_role_word():
    html = '<a href="https://example.com/services/engineering">Engineering Services</a>'
    assert extract_job_links(html, "https://example.com/careers", source()) == []


def test_html_cleaner_removes_navigation_and_scripts():
    html = "<nav>Menu</nav><main><h1>AI Engineer</h1><p>Build agents.</p></main><script>x</script>"
    assert html_to_text(html) == "AI Engineer Build agents."


def test_html_cleaner_decodes_nested_entities_before_parsing_markup():
    html = (
        "&amp;lt;p&amp;gt;Build &amp;lt;span&amp;gt;AI agents."
        "&amp;lt;/span&amp;gt;&amp;lt;/p&amp;gt;"
    )
    assert html_to_text(html) == "Build AI agents."


@pytest.mark.asyncio
async def test_clean_job_returns_stable_json_from_detail_api():
    payload = {
        "name": "AI Automation Engineer",
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Build agents.</p>"}}},
        "location": {"city": "Warsaw", "country": "Poland"},
    }

    async def handler(request):
        return httpx.Response(200, json=payload)

    async with HttpClient(retries=0) as http:
        await http.client.aclose()
        http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        job = DiscoveredJob(
            source_id="example",
            company="Example",
            external_id="1",
            title="AI Engineer",
            url="https://example.com/jobs/1",
            detail_api_url="https://api.example.com/jobs/1",
        )
        cleaned = await clean_job(job, http)

    assert cleaned.title == "AI Automation Engineer"
    assert "Build agents" in cleaned.description
    assert "Warsaw" in cleaned.locations[0]
    assert len(cleaned.raw_sha256) == 64


@pytest.mark.asyncio
async def test_clean_job_strips_html_from_greenhouse_style_content():
    payload = {
        "title": "AI Architect",
        "content": (
            "<h2>What you will do</h2><ul><li>Build AI agents.</li><li>Evaluate models.</li></ul>"
        ),
        "location": {"name": "Warsaw, Poland"},
    }

    job = DiscoveredJob(
        source_id="xebia",
        company="Xebia",
        external_id="123",
        title="AI Architect",
        url="https://job-boards.greenhouse.io/xebiacee/jobs/123",
        metadata={"api_payload": payload},
    )
    async with HttpClient(retries=0) as http:
        cleaned = await clean_job(job, http)

    assert cleaned.description == "What you will do Build AI agents. Evaluate models."
    assert cleaned.analysis_text == "ROLE RESPONSIBILITIES: Build AI agents. Evaluate models."
    assert "<li>" not in cleaned.description
    assert "&lt;li" not in cleaned.description


@pytest.mark.asyncio
async def test_clean_job_strips_html_encoded_as_entities():
    payload = {
        "@type": "JobPosting",
        "title": "AI Architect",
        "description": (
            "&lt;h2&gt;Your role&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Build agents.&lt;/li&gt;&lt;/ul&gt;"
        ),
    }
    job = DiscoveredJob(
        source_id="xebia",
        company="Xebia",
        external_id="124",
        title="AI Architect",
        url="https://job-boards.greenhouse.io/xebiacee/jobs/124",
        metadata={"api_payload": payload},
    )
    async with HttpClient(retries=0) as http:
        cleaned = await clean_job(job, http)

    assert cleaned.description == "Your role Build agents."
    assert "<li>" not in cleaned.description
    assert "&lt;li" not in cleaned.description


@pytest.mark.asyncio
async def test_lever_adapter_reads_official_json_endpoint():
    payload = [
        {
            "id": "role-1",
            "text": "AI Automation Engineer",
            "hostedUrl": "https://jobs.lever.co/example/role-1",
            "description": "<p>Build AI workflows.</p>",
            "categories": {"location": "Remote, Poland"},
        }
    ]

    async def handler(request):
        assert request.url == "https://api.lever.co/v0/postings/example?mode=json"
        return httpx.Response(200, json=payload)

    lever_source = SourceConfig(
        id="lever-example",
        company="Example",
        adapter="lever",
        career_url="https://jobs.lever.co/example/",
        options={"site": "example"},
    )
    async with HttpClient(retries=0) as http:
        await http.client.aclose()
        http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        jobs = await LeverAdapter(lever_source, http).discover()

    assert jobs[0].external_id == "role-1"
    assert jobs[0].location_hint == "Remote, Poland"
    assert jobs[0].metadata["api_payload"]["description"] == "<p>Build AI workflows.</p>"
