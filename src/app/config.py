from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Настройки, передаваемые компонентам при явном запуске приложения."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_provider: str = "OpenInference"
    model: str = "z-ai/glm-5.3-flash"
    planning_model: str = "openai/gpt-6-luna"
    planning_provider: str = "openai"
    planning_reasoning_effort: Literal["low", "medium", "high"] = "medium"
    database_path: Path = Path("../hse_agents_runtime/assistant.sqlite")
    recipes_database_path: Path = Path("data/recipes.sqlite")
    products_database_path: Path = Path("data/products.sqlite")
    sources_database_path: Path = Path("data/sources.sqlite")
    vkusvill_mcp_url: str = "https://mcp.vkusvill.ru/mcp"
    request_limit: int = Field(default=12, ge=1)
    turn_timeout_seconds: float = Field(default=600, gt=0)
    model_timeout_seconds: float = Field(default=180, gt=0)
    context_window_tokens: int = Field(default=256000, gt=0)
    response_reserve_tokens: int = Field(default=16000, gt=0)
    token_count_margin: int = Field(default=8000, ge=0)
    compaction_threshold_tokens: int = Field(default=200000, gt=0)
    summary_max_tokens: int = Field(default=8000, gt=0)
    compaction_timeout_seconds: float = Field(default=180, gt=0)
    compaction_request_limit: int = Field(default=2, ge=1, le=2)
    tokenizer_cache_path: Path = Path("../hse_agents_runtime/tokenizer")
    guard_model: str = "openai/gpt-oss-safeguard-20b"
    guard_timeout_seconds: float = Field(default=60, gt=0)
    integration_timeout_seconds: float = Field(default=60, gt=0)
    session_days: int = Field(default=30, gt=0)
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    frontend_path: Path = Path("frontend/dist")
    log_level: str = "INFO"

    @property
    def max_input_tokens(self) -> int:
        """Возвращает предел входа с резервом ответа и погрешности."""
        return self.context_window_tokens - self.response_reserve_tokens - self.token_count_margin

    @model_validator(mode="after")
    def validate_context_budget(self) -> "Config":
        """Проверяет согласованность порога, сводки и общего окна."""
        if not self.summary_max_tokens < self.compaction_threshold_tokens <= self.max_input_tokens:
            raise ValueError("Требуется summary_max_tokens < compaction_threshold_tokens <= max_input_tokens")
        return self


@lru_cache
def get_config() -> Config:
    """Загружает настройки без побочных эффектов при импорте.

    :return: Настройки окружения и необязательного .env
    """
    return Config()
