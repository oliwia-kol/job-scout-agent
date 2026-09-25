"""OpenAI-compatible client for a local llama.cpp server."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LocalLlmError(RuntimeError):
    """A local inference request failed or returned an unusable response."""


@dataclass(frozen=True)
class StructuredLlmResponse(Generic[SchemaT]):
    value: SchemaT
    raw_text: str
    elapsed_ms: int
    retries: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LocalLlmClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def __aenter__(self) -> LocalLlmClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self.client.aclose()

    @property
    def health_url(self) -> str:
        root = self.base_url.removesuffix("/v1")
        return f"{root}/health"

    async def health(self) -> dict:
        try:
            response = await self.client.get(self.health_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalLlmError(f"local llama.cpp health check failed: {exc}") from exc
        if payload.get("status") not in {"ok", "ready"}:
            raise LocalLlmError(f"local llama.cpp is not ready: {payload!r}")
        return payload

    async def structured_completion(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
        schema_name: str,
        seed: int = 42,
        temperature: float = 0,
        max_tokens: int | None = None,
        strict_schema: bool = True,
        instruction_language: Literal["en", "pl"] = "en",
        validate: Callable[[SchemaT], None] | None = None,
        json_schema_override: dict | None = None,
    ) -> StructuredLlmResponse[SchemaT]:
        schema_json = json.dumps(schema.model_json_schema(), sort_keys=True)
        schema_instruction = (
            "Zwróć wyłącznie JSON zgodny z poniższym schematem. Wszystkie wartości "
            "przeznaczone dla użytkownika zachowaj w języku wymaganym w prompcie. Schemat: "
            if instruction_language == "pl"
            else "Return only JSON matching this schema: "
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    user_prompt
                    if strict_schema
                    else f"{user_prompt}\n\n{schema_instruction}{schema_json}"
                ),
            },
        ]
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(2):
            response_format = (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": json_schema_override or schema.model_json_schema(),
                    },
                }
                if attempt == 0 and strict_schema
                else {"type": "json_object"}
            )
            request = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "seed": seed,
                "response_format": response_format,
            }
            if max_tokens is not None:
                request["max_tokens"] = max_tokens
            try:
                response = await self.client.post(f"{self.base_url}/chat/completions", json=request)
                response.raise_for_status()
                payload = response.json()
                raw_text = payload["choices"][0]["message"]["content"]
                value = schema.model_validate_json(_json_payload(raw_text))
                if validate:
                    validate(value)
                usage = payload.get("usage") or {}
                return StructuredLlmResponse(
                    value=value,
                    raw_text=raw_text,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    retries=attempt,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
                last_error = exc
                if attempt == 0:
                    correction = (
                        (
                            "Wygeneruj odpowiedź od nowa po polsku. Nie kopiuj angielskiej "
                            "wersji. Zwróć wyłącznie poprawiony JSON. Błąd walidacji: "
                        )
                        if instruction_language == "pl" and "must be in Polish" in str(exc)
                        else (
                            "Poprzedni JSON nie przeszedł walidacji. Zwróć wyłącznie "
                            "poprawiony JSON zgodny ze schematem. Nie zmieniaj "
                            "wymaganego języka polskiego. Błąd walidacji: "
                            if instruction_language == "pl"
                            else (
                                "Your previous JSON failed validation. Return only "
                                "corrected JSON matching the schema. Validation error: "
                            )
                        )
                    )
                    if not (
                        instruction_language == "pl" and "must be in Polish" in str(exc)
                    ):
                        messages.append(
                            {"role": "assistant", "content": _response_text(locals())}
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                correction
                                + f"{exc}. {schema_instruction}{schema_json}"
                            ),
                        }
                    )
        raise LocalLlmError(f"structured response failed after one retry: {last_error}")


def _response_text(context: dict) -> str:
    """Recover the response text for a repair turn without masking the original error."""
    raw_text = context.get("raw_text")
    if isinstance(raw_text, str):
        return raw_text
    payload = context.get("payload")
    if isinstance(payload, dict):
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return json.dumps(payload)
    return ""


def _json_payload(raw_text: str) -> str:
    """Accept a single JSON object optionally wrapped in a Markdown JSON fence."""
    stripped = raw_text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    object_start = stripped.find("{")
    if object_start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(stripped[object_start:])
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return stripped
