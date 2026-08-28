import json

import httpx
import pytest
from pydantic import BaseModel

from job_scout.local_llm import LocalLlmClient, LocalLlmError


class SmokeResult(BaseModel):
    status: str
    count: int


@pytest.mark.asyncio
async def test_health_uses_llama_server_root_endpoint():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://127.0.0.1:8000/v1", client=http)
        assert await client.health() == {"status": "ok"}


@pytest.mark.asyncio
async def test_structured_completion_sends_json_schema_and_returns_usage():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["seed"] == 42
        assert "max_tokens" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"status":"ok","count":1}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="qwen",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
        )
    assert result.value == SmokeResult(status="ok", count=1)
    assert result.retries == 0
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 8


@pytest.mark.asyncio
async def test_structured_completion_includes_explicit_token_limit_only_when_requested():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 1234
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"status":"ok","count":1}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        await client.structured_completion(
            model="qwen",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
            max_tokens=1234,
        )


@pytest.mark.asyncio
async def test_invalid_json_gets_exactly_one_repair_attempt():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = '{"status":"ok"}' if len(requests) == 1 else '{"status":"ok","count":2}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="qwen",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
        )
    assert result.value.count == 2
    assert result.retries == 1
    assert len(requests) == 2
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert "Validation error" in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_schema_http_error_retries_with_json_object_mode():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={"error": "unsupported schema grammar"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"status":"ok","count":3}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="bielik",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
        )

    assert result.value.count == 3
    assert result.retries == 1
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[1]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_json_object_compatibility_mode_includes_schema_in_prompt():
    request_payload = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        request_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"status":"ok","count":4}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="bielik",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
            strict_schema=False,
        )

    assert result.value.count == 4
    assert request_payload["response_format"] == {"type": "json_object"}
    assert '"required": ["status", "count"]' in request_payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_polish_json_mode_localizes_schema_and_repair_instructions():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = "{}" if len(requests) == 1 else '{"status":"ok","count":7}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="bielik",
            system_prompt="Odpowiadaj po polsku.",
            user_prompt="Test.",
            schema=SmokeResult,
            schema_name="smoke_result",
            strict_schema=False,
            instruction_language="pl",
        )

    assert result.value.count == 7
    assert "Zwróć wyłącznie JSON" in requests[0]["messages"][1]["content"]
    assert "Nie zmieniaj wymaganego języka polskiego" in requests[1]["messages"][-1][
        "content"
    ]


@pytest.mark.asyncio
async def test_polish_language_retry_does_not_echo_english_invalid_response():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = (
            '{"status":"english","count":1}'
            if len(requests) == 1
            else '{"status":"polski","count":2}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def require_polish(value: SmokeResult) -> None:
        if value.status != "polski":
            raise ValueError("response must be in Polish")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="bielik",
            system_prompt="Odpowiadaj po polsku.",
            user_prompt="Test.",
            schema=SmokeResult,
            schema_name="smoke_result",
            strict_schema=False,
            instruction_language="pl",
            validate=require_polish,
        )

    assert result.value.status == "polski"
    assert len(requests[1]["messages"]) == 3
    assert all(
        message["role"] != "assistant" for message in requests[1]["messages"]
    )


@pytest.mark.asyncio
async def test_structured_completion_accepts_json_markdown_fence():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"status":"ok","count":5}\n```'
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="bielik",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
            strict_schema=False,
        )

    assert result.value.count == 5
    assert result.retries == 0


@pytest.mark.asyncio
async def test_structured_completion_ignores_trailing_model_commentary():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"status":"ok","count":6}\n'
                                "Powyżej znajduje się żądany obiekt."
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        result = await client.structured_completion(
            model="bielik",
            system_prompt="Return structured data.",
            user_prompt="Smoke test.",
            schema=SmokeResult,
            schema_name="smoke_result",
            strict_schema=False,
        )

    assert result.value.count == 6


@pytest.mark.asyncio
async def test_second_invalid_response_fails_the_item():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = LocalLlmClient("http://local/v1", client=http)
        with pytest.raises(LocalLlmError, match="after one retry"):
            await client.structured_completion(
                model="qwen",
                system_prompt="Return structured data.",
                user_prompt="Smoke test.",
                schema=SmokeResult,
                schema_name="smoke_result",
            )
