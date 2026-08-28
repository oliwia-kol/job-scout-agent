from unittest.mock import AsyncMock, patch

import pytest

from job_scout.collector import collect_sources
from job_scout.sources import SourceConfig


@pytest.mark.asyncio
async def test_full_pipeline_collection():
    # Setup mock sources
    source_html = SourceConfig(
        id="test_html",
        company="Test HTML",
        adapter="html",
        career_url="https://example.com/careers",
    )
    sources = [source_html]

    # Mock HttpClient responses
    mock_html = """
    <html>
        <head><meta property="og:locality" content="Warsaw" /></head>
        <body>
            <h1>Machine Learning Engineer - Remote</h1>
            <main>
                <a href="https://example.com/job/123">Machine Learning Engineer - Remote</a>
                <p>We are looking for an ML engineer with PyTorch experience.</p>
            </main>
        </body>
    </html>
    """

    mock_detail = """
    <html>
        <head><meta property="og:locality" content="Warsaw" /></head>
        <body>
            <h1>Machine Learning Engineer</h1>
            <main>
                <p>We are looking for an ML engineer with PyTorch experience.</p>
            </main>
        </body>
    </html>
    """

    class MockResponse:
        def __init__(self, text, url=None):
            self.text = text
            self.status_code = 200
            self.url = url or "https://example.com/job/123"

        def json(self):
            return {}

        def raise_for_status(self):
            pass

    async def mock_get(url, **kwargs):
        if url == "https://example.com/careers":
            return MockResponse(mock_html, url=url)
        return MockResponse(mock_detail, url=url)

    class MockHttpClient:
        def __init__(self, *args, **kwargs):
            self.client = AsyncMock()
            self.get = AsyncMock(side_effect=mock_get)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("job_scout.collector.HttpClient", MockHttpClient):
        result = await collect_sources(sources, limit_per_source=5, apply_prefilter=True)

    assert not result.errors
    assert len(result.offers) == 1

    offer = result.offers[0]
    assert offer.company == "Test HTML"
    assert offer.title == "Machine Learning Engineer"  # sanitized
    assert "Warsaw" in offer.locations
    assert "PyTorch" in offer.description
