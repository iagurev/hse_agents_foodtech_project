---
name: configuration_management
description: Requires centralized environment-based application settings in src/app/config.py using pydantic-settings.
---

# Configuration Standards

Declare, load, and validate environment-based application settings in `src/app/config.py` in the application package using `pydantic-settings`. Application modules must not call `os.getenv()` or read `os.environ` directly elsewhere. Pass validated settings into components instead.

## Example Pattern

This example contains only illustrative common settings; it does not prescribe application functionality or create an existing module:

```python
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent


class CommonSettings(BaseModel):
    mode: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_nested_delimiter="__",
    )

    common: CommonSettings = Field(default_factory=CommonSettings)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Загружает и возвращает общую конфигурацию приложения.

    :return: Проверенные настройки приложения
    """
    return Config()
```

Load `.env` from the launch directory; resolve data paths explicitly when launching elsewhere. An application entry point imports `get_config` with `from app.config import get_config`. Load settings at startup and pass them to components. Avoid import-time settings initialization so importing pure logic does not require runtime credentials.

## Environment And Validation

- Process environment values take precedence over the optional `.env` file. A nested override for the example is `COMMON__LOG_LEVEL=DEBUG`.
- Starting without `.env` is valid when defaults and process environment supply all required fields. Do not require a shell `source` step.
- Add genuinely required values as required fields and report validation errors at startup. Do not invent placeholder credentials or require settings for integrations the application does not use.
- Represent secrets with `SecretStr` where applicable. Maintain safe example environment files with variable names and non-secret examples when actual settings are introduced; never commit real credentials.
- Tests may set/unset environment variables in isolated fixtures or instantiate `Config(_env_file=None, ...)` with controlled values. Inject those instances into tested components; reset the cached loader if a test deliberately exercises it.

Docker supplies the process environment through its actual runtime configuration. The same settings model validates it; a local `.env` file does not need to be copied into an image.
