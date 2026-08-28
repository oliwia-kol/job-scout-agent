import httpx
import pytest

from job_scout.ats_scrapers import EpamAdapter, HttpClient
from job_scout.sources import SourceConfig


@pytest.mark.asyncio
async def test_epam_adapter_extracts_vacancies_not_category_links():
    html = """
    <a href="/en/jobs/data-scientist">Data Scientist Jobs</a>
    <script>{"seo":{"url":"/en/vacancy/ai-engineer-blt123_en",
    "title":"Careers for Senior AI Engineer | EPAM"}}</script>
    """

    async def handler(request):
        return httpx.Response(200, text=html, request=request)

    source = SourceConfig(
        id="epam",
        company="EPAM",
        adapter="epam",
        career_url="https://careers.epam.com/en/jobs/poland?search=ai",
    )
    async with HttpClient(retries=0) as http:
        await http.client.aclose()
        http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        jobs = await EpamAdapter(source, http).discover()

    assert len(jobs) == 1
    assert jobs[0].title == "Senior AI Engineer"
    assert "/vacancy/" in str(jobs[0].url)
