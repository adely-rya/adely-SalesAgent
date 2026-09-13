"""Environment configuration; loading does not require credentials."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    openai_api_key: SecretStr = SecretStr('')
    discord_webhook_url: SecretStr = SecretStr('')
    database_url: str = 'sqlite:///data/sales.db'
    discovery_model: str = 'gpt-5.6-luna'
    scoring_model: str = 'gpt-5.6-luna'
    strategy_model: str = 'gpt-5.6-terra'
    timezone: str = 'Asia/Tokyo'
    daily_run_hour: int = Field(8, ge=0, le=23)
    daily_run_minute: int = Field(0, ge=0, le=59)
    top_candidates: int = Field(5, ge=1, le=5)
    log_level: str = 'INFO'
    discovery_prompt_version: str = 'v2'
    scoring_prompt_version: str = 'v2'
    strategy_prompt_version: str = 'v1'
    prompts_dir: Path = Path(__file__).resolve().parent.parent / 'prompts'
    openai_timeout_seconds: float = Field(120, gt=0)
    strategy_web_search: bool = False

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator('log_level')
    @classmethod
    def valid_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
            raise ValueError('Invalid LOG_LEVEL')
        return value

    @classmethod
    def from_env(cls) -> 'Settings':
        load_dotenv()
        return cls(**{name: os.environ[name.upper()] for name in cls.model_fields
                      if name.upper() in os.environ})

    @property
    def has_api_key(self) -> bool:
        return configured(self.openai_api_key.get_secret_value())


def configured(value: str) -> bool:
    return value.strip().lower() not in {'', 'hogehoge'}
