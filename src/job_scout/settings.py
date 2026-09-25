"""Runtime settings. Secrets are read only when the process starts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/job_scout.db")
    sources_path: Path = Path("config/sources.json")
    local_llm_base_url: str = "http://127.0.0.1:8000/v1"
    local_llm_model: str = "Qwen3.5-9B-Q4_K_M.gguf"
    local_llm_model_path: Path = Path("models/Qwen3.5-9B-Q4_K_M.gguf")
    local_llm_context_size: int = 4096
    local_llm_reasoning: bool = False
    local_llm_reasoning_budget: int | None = None
    local_llm_stage_timeout_seconds: float = 1800.0
    evaluation_model: str = "Qwen3.5-9B-Q4_K_M.gguf"
    evaluation_model_path: Path = Path("models/Qwen3.5-9B-Q4_K_M.gguf")
    scout_llm_model: str = "Qwen3.5-9B-Q4_K_M.gguf"
    scout_llm_model_path: Path = Path("models/Qwen3.5-9B-Q4_K_M.gguf")
    career_llm_model: str = "Bielik-11B-v3.0-Instruct.Q4_K_M.gguf"
    career_llm_model_path: Path = Path("models/Bielik-11B-v3.0-Instruct.Q4_K_M.gguf")
    career_llm_base_url: str = "http://127.0.0.1:8001/v1"
    career_llm_context_size: int = 8192
    career_llm_startup_timeout_seconds: float = 120.0
    career_llm_autostart: bool = True
    career_llm_speculative_type: str | None = None
    lm_studio_base_url: str = "http://127.0.0.1:1234/v1"
    llama_server_executable: str = "llama-server"
    mlflow_tracking_uri: str = "file:./data/mlruns"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notion_api_key: str | None = None
    notion_database_id: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            database_path=Path(os.getenv("JOB_SCOUT_DATABASE_PATH", "data/job_scout.db")),
            sources_path=Path(os.getenv("JOB_SCOUT_SOURCES_PATH", "config/sources.json")),
            local_llm_base_url=os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
            local_llm_model=os.getenv("LOCAL_LLM_MODEL", "Qwen3.5-9B-Q4_K_M.gguf"),
            local_llm_model_path=Path(
                os.getenv("LOCAL_LLM_MODEL_PATH", "models/Qwen3.5-9B-Q4_K_M.gguf")
            ),
            local_llm_context_size=int(os.getenv("LOCAL_LLM_CONTEXT_SIZE", "4096")),
            local_llm_reasoning=os.getenv("LOCAL_LLM_REASONING", "false").lower()
            in {"1", "true", "yes", "on"},
            local_llm_reasoning_budget=_optional_int("LOCAL_LLM_REASONING_BUDGET"),
            local_llm_stage_timeout_seconds=float(
                os.getenv("LOCAL_LLM_STAGE_TIMEOUT_SECONDS", "1800")
            ),
            evaluation_model=os.getenv("JOB_SCOUT_EVALUATION_MODEL", "Qwen3.5-9B-Q4_K_M.gguf"),
            evaluation_model_path=Path(
                os.getenv("JOB_SCOUT_EVALUATION_MODEL_PATH", "models/Qwen3.5-9B-Q4_K_M.gguf")
            ),
            scout_llm_model=os.getenv("SCOUT_LLM_MODEL", "Qwen3.5-9B-Q4_K_M.gguf"),
            scout_llm_model_path=Path(
                os.getenv("SCOUT_LLM_MODEL_PATH", "models/Qwen3.5-9B-Q4_K_M.gguf")
            ),
            career_llm_model=os.getenv("CAREER_LLM_MODEL", "Bielik-11B-v3.0-Instruct.Q4_K_M.gguf"),
            career_llm_model_path=Path(
                os.getenv(
                    "CAREER_LLM_MODEL_PATH",
                    "models/Bielik-11B-v3.0-Instruct.Q4_K_M.gguf",
                )
            ),
            career_llm_base_url=os.getenv("CAREER_LLM_BASE_URL", "http://127.0.0.1:8001/v1"),
            career_llm_context_size=int(os.getenv("CAREER_LLM_CONTEXT_SIZE", "8192")),
            career_llm_startup_timeout_seconds=float(
                os.getenv("CAREER_LLM_STARTUP_TIMEOUT_SECONDS", "120")
            ),
            career_llm_autostart=os.getenv("CAREER_LLM_AUTOSTART", "true").lower()
            in {"1", "true", "yes", "on"},
            career_llm_speculative_type=(os.getenv("CAREER_LLM_SPECULATIVE_TYPE") or None),
            lm_studio_base_url=os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1"),
            llama_server_executable=os.getenv("LLAMA_SERVER_EXECUTABLE", "llama-server"),
            mlflow_tracking_uri=os.getenv("MLFLOW_TRACKING_URI", "file:./data/mlruns"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            notion_api_key=os.getenv("NOTION_API_KEY"),
            notion_database_id=os.getenv("NOTION_DATABASE_ID"),
        )


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value and value.strip() else None
