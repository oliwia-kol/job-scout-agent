import httpx
import pytest

from job_scout.model_runtime import probe_runtime


@pytest.mark.asyncio
async def test_lm_studio_probe_uses_openai_models_endpoint():
    async def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen-test"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status = await probe_runtime(
            "lm_studio", "http://127.0.0.1:1234/v1", client=client
        )
    assert status.ready is True
    assert status.models == ("qwen-test",)


@pytest.mark.asyncio
async def test_runtime_probe_returns_diagnostic_instead_of_raising():
    async def handler(request):
        return httpx.Response(503, text="not ready")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status = await probe_runtime(
            "lm_studio", "http://127.0.0.1:1234/v1", client=client
        )
    assert status.ready is False
    assert "503" in status.detail
