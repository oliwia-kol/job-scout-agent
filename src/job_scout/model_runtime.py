"""Runtime-neutral access to local OpenAI-compatible model servers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from .local_llm import LocalLlmClient, LocalLlmError

RuntimeKind = Literal["llama_cpp", "lm_studio"]


@dataclass(frozen=True)
class RuntimeStatus:
    kind: RuntimeKind
    base_url: str
    ready: bool
    models: tuple[str, ...]
    detail: str


async def probe_runtime(
    kind: RuntimeKind,
    base_url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> RuntimeStatus:
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=5)
    normalized = base_url.rstrip("/")
    try:
        if kind == "llama_cpp":
            async with LocalLlmClient(
                normalized, client=http
            ) as local_client:
                await local_client.health()
            models = await _list_models(http, normalized)
        else:
            models = await _list_models(http, normalized)
        return RuntimeStatus(
            kind=kind,
            base_url=normalized,
            ready=True,
            models=tuple(models),
            detail=f"{len(models)} model(s) visible",
        )
    except (httpx.HTTPError, ValueError, LocalLlmError) as exc:
        return RuntimeStatus(
            kind=kind,
            base_url=normalized,
            ready=False,
            models=(),
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_client:
            await http.aclose()


async def _list_models(client: httpx.AsyncClient, base_url: str) -> list[str]:
    response = await client.get(f"{base_url}/models")
    response.raise_for_status()
    payload = response.json()
    return [str(item["id"]) for item in payload.get("data", []) if item.get("id")]
