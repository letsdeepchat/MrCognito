"""Runtime configuration. Every knob is an env var prefixed ``COGNITO_``."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COGNITO_", env_file=".env", extra="ignore")

    # --- persistence -------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./cognito.db"

    # --- engines -----------------------------------------------------------
    stockfish_path: str = "stockfish"
    stockfish_threads: int = 1
    stockfish_hash_mb: int = 64
    stockfish_pool_size: int = 2
    analysis_depth: int = 14
    analysis_multipv: int = 4

    # "maia2" uses the real Maia-2 network (needs torch + weights);
    # "heuristic" is a rating-aware softmax over engine MultiPV + human priors.
    human_model: Literal["maia2", "heuristic"] = "heuristic"
    maia2_model_type: Literal["rapid", "blitz"] = "rapid"
    maia2_weights_dir: str = "./maia2_models"

    # --- LLM coach ---------------------------------------------------------
    # "rule" = deterministic offline Socratic coach (default, no network)
    # "anthropic" = Claude via the Anthropic SDK
    # "openai_compat" = any OpenAI-compatible server (vLLM serving the LoRA adapter, Ollama, ...)
    llm_provider: Literal["rule", "anthropic", "openai_compat"] = "rule"
    llm_model: str = "claude-opus-5-5"
    llm_base_url: str = "http://localhost:8001/v1"
    llm_api_key: str = ""
    llm_max_tool_rounds: int = 4

    # --- RAG ---------------------------------------------------------------
    rag_backend: Literal["memory", "pgvector"] = "memory"

    # --- protocol ----------------------------------------------------------
    cooldown_hours: int = 24
    allowed_budgets: tuple[int, ...] = (10, 20, 30)
    # Grace on top of the budget before a dangling ACTIVE session is force-locked.
    session_grace_minutes: int = 5

    # --- integrity (anti engine-lookup) -----------------------------------
    integrity_window: int = 8
    integrity_top1_threshold: float = 0.85
    integrity_latency_cv_threshold: float = 0.25

    # --- vision ------------------------------------------------------------
    vision_onnx_path: str = ""

    cors_origins: str = "http://localhost:5173,http://localhost:8080,https://localhost,capacitor://localhost"


@lru_cache
def get_settings() -> Settings:
    return Settings()
