"""Lifecycle management for the local llama.cpp server."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

import httpx

from .local_llm import LocalLlmClient, LocalLlmError

ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]
HealthProbe = Callable[[], Awaitable[dict]]


class LlamaServerError(RuntimeError):
    """The managed local model server failed to start or stop cleanly."""


def build_llama_server_command(
    executable: str,
    model_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    context_size: int = 16384,
    reasoning: bool = False,
    reasoning_budget: int | None = None,
    speculative_type: str | None = None,
) -> list[str]:
    command = [
        executable,
        "-m",
        str(model_path),
        "--host",
        host,
        "--port",
        str(port),
        "-c",
        str(context_size),
        "-ngl",
        "99",
        "-np",
        "1",
        "--reasoning",
        "on" if reasoning else "off",
    ]
    if reasoning:
        command.extend(["--reasoning-format", "deepseek"])
        if reasoning_budget is not None:
            command.extend(["--reasoning-budget", str(reasoning_budget)])
    if speculative_type:
        command.extend(["--spec-type", speculative_type])
    return command


class LlamaServerManager:
    def __init__(
        self,
        *,
        executable: str,
        model_path: Path,
        base_url: str,
        context_size: int = 16384,
        reasoning: bool = False,
        reasoning_budget: int | None = None,
        speculative_type: str | None = None,
        startup_timeout_seconds: float = 60,
        poll_interval_seconds: float = 0.25,
        log_path: Path | None = None,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        health_probe: HealthProbe | None = None,
    ) -> None:
        self.executable = executable
        self.model_path = model_path
        self.base_url = base_url.rstrip("/")
        self.context_size = context_size
        self.reasoning = reasoning
        self.reasoning_budget = reasoning_budget
        self.speculative_type = speculative_type
        self.startup_timeout_seconds = startup_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.log_path = log_path
        self.process_factory = process_factory
        self.health_probe = health_probe or self._probe_health
        self.process: asyncio.subprocess.Process | None = None
        self._log_handle = None

    async def __aenter__(self) -> LlamaServerManager:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def _probe_health(self) -> dict:
        async with LocalLlmClient(self.base_url, timeout_seconds=2) as client:
            return await client.health()

    def command(self) -> list[str]:
        port = httpx.URL(self.base_url).port or 8000
        host = httpx.URL(self.base_url).host or "127.0.0.1"
        return build_llama_server_command(
            self.executable,
            self.model_path,
            host=host,
            port=port,
            context_size=self.context_size,
            reasoning=self.reasoning,
            reasoning_budget=self.reasoning_budget,
            speculative_type=self.speculative_type,
        )

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            raise LlamaServerError("llama-server is already managed by this instance")
        if not self.model_path.is_file():
            raise LlamaServerError(f"local model file does not exist: {self.model_path}")
        stdout = asyncio.subprocess.DEVNULL
        stderr = asyncio.subprocess.DEVNULL
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.log_path.open("ab")
            stdout = self._log_handle
            stderr = asyncio.subprocess.STDOUT
        try:
            self.process = await self.process_factory(
                *self.command(),
                stdout=stdout,
                stderr=stderr,
            )
        except OSError as exc:
            self._close_log()
            raise LlamaServerError(f"could not start llama-server: {exc}") from exc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout_seconds
        while loop.time() < deadline:
            if self.process.returncode is not None:
                code = self.process.returncode
                self.process = None
                self._close_log()
                raise LlamaServerError(f"llama-server exited during startup with code {code}")
            try:
                await self.health_probe()
                return
            except (LocalLlmError, httpx.HTTPError):
                await asyncio.sleep(self.poll_interval_seconds)
        await self.stop()
        raise LlamaServerError(
            f"llama-server was not healthy after {self.startup_timeout_seconds:g}s"
        )

    async def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            self._close_log()
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
        finally:
            self._close_log()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None


RuntimeState = Literal["idle", "starting", "ready", "error", "unavailable"]


class OnDemandLlamaRuntime:
    """Concurrency-safe, application-owned llama-server started on first model request."""

    def __init__(self, manager: LlamaServerManager) -> None:
        self.manager = manager
        self._lock = asyncio.Lock()
        self._state: RuntimeState = (
            "idle" if manager.model_path.is_file() else "unavailable"
        )
        self._last_error: str | None = None

    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "ready": self._state == "ready",
            "model": self.manager.model_path.name,
            "model_available": self.manager.model_path.is_file(),
            "managed": bool(
                self.manager.process and self.manager.process.returncode is None
            ),
            "detail": self._last_error,
        }

    async def refresh_status(self) -> dict:
        if await self._is_healthy():
            self._state = "ready"
            self._last_error = None
        elif not self.manager.model_path.is_file():
            self._state = "unavailable"
            self._last_error = "Brak lokalnego pliku modelu Bielik."
        elif self._state == "ready":
            self._state = "idle"
        return self.snapshot()

    async def ensure_ready(self) -> dict:
        if await self._is_healthy():
            self._state = "ready"
            self._last_error = None
            return self.snapshot()

        async with self._lock:
            if await self._is_healthy():
                self._state = "ready"
                self._last_error = None
                return self.snapshot()
            self._state = "starting"
            self._last_error = None
            try:
                await self.manager.start()
            except (LlamaServerError, OSError) as exc:
                # A compatible server may have won the port race while this process started.
                if await self._is_healthy():
                    self._state = "ready"
                    return self.snapshot()
                self._state = "error"
                self._last_error = str(exc)
                raise LlamaServerError(str(exc)) from exc
            self._state = "ready"
            return self.snapshot()

    async def stop(self) -> None:
        await self.manager.stop()
        if self._state != "unavailable":
            self._state = "idle"

    async def _is_healthy(self) -> bool:
        try:
            await self.manager.health_probe()
            return True
        except (LocalLlmError, httpx.HTTPError, OSError):
            return False
