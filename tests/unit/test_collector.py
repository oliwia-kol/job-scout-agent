import httpx
import pytest

from job_scout.ats_scrapers import HttpClient
from job_scout.collector import collect_sources
from job_scout.sources import SourceConfig


@pytest.mark.asyncio
async def test_explicitly_valid_empty_source_is_healthy(monkeypatch):
    source = SourceConfig(
        id="between-searches",
        company="Between Searches",
        adapter="html",
        career_url="https://example.com/careers",
        options={"empty_is_valid": True},
    )

    async def handler(request):
        return httpx.Response(200, text="<h2>No open roles right now</h2>")

    original_enter = HttpClient.__aenter__

    async def enter(client):
        entered = await original_enter(client)
        await entered.client.aclose()
        entered.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return entered

    monkeypatch.setattr(HttpClient, "__aenter__", enter)
    result = await collect_sources([source])
    assert result.errors == []
    assert result.observations[0].status == "ok"
    assert result.observations[0].discovered_count == 0
