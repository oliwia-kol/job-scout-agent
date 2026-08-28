from pathlib import Path

import pytest

from job_scout.llama_server import (
    LlamaServerError,
    LlamaServerManager,
    OnDemandLlamaRuntime,
    build_llama_server_command,
)
from job_scout.local_llm import LocalLlmError


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_command_is_local_metal_16k_and_single_slot():
    command = build_llama_server_command("llama-server", Path("models/gemma.gguf"))
    assert command[:3] == ["llama-server", "-m", "models/gemma.gguf"]
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("-c") + 1] == "16384"
    assert command[command.index("-ngl") + 1] == "99"
    assert command[command.index("-np") + 1] == "1"
    assert command[command.index("--reasoning") + 1] == "off"
    assert "--chat-template-kwargs" not in command


def test_reasoning_budget_is_omitted_when_unset():
    command = build_llama_server_command(
        "llama-server", Path("models/deepseek.gguf"), reasoning=True
    )
    assert "--reasoning-budget" not in command


@pytest.mark.asyncio
async def test_manager_waits_for_health_and_stops_process(tmp_path):
    model = tmp_path / "gemma.gguf"
    model.write_bytes(b"gguf")
    process = FakeProcess()
    calls = 0

    async def factory(*_args, **_kwargs):
        return process

    async def health():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LocalLlmError("loading")
        return {"status": "ok"}

    manager = LlamaServerManager(
        executable="llama-server",
        model_path=model,
        base_url="http://127.0.0.1:8000/v1",
        poll_interval_seconds=0,
        process_factory=factory,
        health_probe=health,
    )
    async with manager:
        assert process.returncode is None
        assert calls == 2
    assert process.terminated is True


@pytest.mark.asyncio
async def test_missing_model_fails_before_process_start(tmp_path):
    async def factory(*_args, **_kwargs):
        raise AssertionError("process must not start")

    manager = LlamaServerManager(
        executable="llama-server",
        model_path=tmp_path / "missing.gguf",
        base_url="http://127.0.0.1:8000/v1",
        process_factory=factory,
    )
    with pytest.raises(LlamaServerError, match="does not exist"):
        await manager.start()


@pytest.mark.asyncio
async def test_early_process_exit_is_reported(tmp_path):
    model = tmp_path / "gemma.gguf"
    model.write_bytes(b"gguf")

    async def factory(*_args, **_kwargs):
        return FakeProcess(returncode=1)

    manager = LlamaServerManager(
        executable="llama-server",
        model_path=model,
        base_url="http://127.0.0.1:8000/v1",
        process_factory=factory,
    )
    with pytest.raises(LlamaServerError, match="code 1"):
        await manager.start()


@pytest.mark.asyncio
async def test_on_demand_runtime_starts_once_and_reuses_healthy_server(tmp_path):
    model = tmp_path / "bielik.gguf"
    model.write_bytes(b"gguf")
    process = FakeProcess()
    health_calls = 0
    process_starts = 0

    async def factory(*_args, **_kwargs):
        nonlocal process_starts
        process_starts += 1
        return process

    async def health():
        nonlocal health_calls
        health_calls += 1
        if health_calls < 3:
            raise LocalLlmError("not started")
        return {"status": "ok"}

    runtime = OnDemandLlamaRuntime(
        LlamaServerManager(
            executable="llama-server",
            model_path=model,
            base_url="http://127.0.0.1:8001/v1",
            process_factory=factory,
            health_probe=health,
            poll_interval_seconds=0,
        )
    )

    first = await runtime.ensure_ready()
    second = await runtime.ensure_ready()

    assert first["ready"] is second["ready"] is True
    assert first["managed"] is True
    assert process_starts == 1
    await runtime.stop()
    assert process.terminated is True


@pytest.mark.asyncio
async def test_on_demand_runtime_attaches_to_existing_healthy_server(tmp_path):
    model = tmp_path / "bielik.gguf"
    model.write_bytes(b"gguf")

    async def factory(*_args, **_kwargs):
        raise AssertionError("healthy external server must not be restarted")

    async def health():
        return {"status": "ok"}

    runtime = OnDemandLlamaRuntime(
        LlamaServerManager(
            executable="llama-server",
            model_path=model,
            base_url="http://127.0.0.1:8001/v1",
            process_factory=factory,
            health_probe=health,
        )
    )

    status = await runtime.ensure_ready()

    assert status["ready"] is True
    assert status["managed"] is False


@pytest.mark.asyncio
async def test_on_demand_runtime_reports_missing_model(tmp_path):
    runtime = OnDemandLlamaRuntime(
        LlamaServerManager(
            executable="llama-server",
            model_path=tmp_path / "missing.gguf",
            base_url="http://127.0.0.1:8001/v1",
        )
    )

    status = await runtime.refresh_status()

    assert status["state"] == "unavailable"
    assert status["model_available"] is False
