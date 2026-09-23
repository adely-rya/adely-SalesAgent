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
    # `None` delegates to the model's default reasoning level.
    discovery_reasoning_effort: str | None = 'xhigh'
    scoring_reasoning_effort: str | None = None
    strategy_reasoning_effort: str | None = None
    timezone: str = 'Asia/Tokyo'
    daily_run_hour: int = Field(8, ge=0, le=23)
    daily_run_minute: int = Field(0, ge=0, le=59)
    top_candidates: int = Field(5, ge=1, le=5)
    log_level: str = 'INFO'
    discovery_prompt_version: str = 'v7'
    scoring_prompt_version: str = 'v7'
    strategy_prompt_version: str = 'v1'
    prompts_dir: Path = Path(__file__).resolve().parent.parent / 'prompts'
    openai_timeout_seconds: float = Field(120, gt=0)
    strategy_web_search: bool = False
    collector_timeout_seconds: float = Field(15, gt=0, le=60)
    collector_max_events_per_source: int = Field(20, ge=1, le=100)
    fixed_collector_interval_hours: int = Field(3, ge=1, le=24)
    fixed_discovery_model: str = 'gpt-5.6-luna'
    fixed_discovery_reasoning_effort: str | None = 'medium'
    fixed_discovery_prompt_version: str = 'v1'
    fixed_discovery_batch_size: int = Field(20, ge=1, le=100)
    source_prefilter_batch_size: int = Field(100, ge=1, le=1000)
    fixed_discovery_include_hold_events: bool = True
    atpress_prefilter_pass_score: float = 2.0
    atpress_prefilter_drop_score: float = -1.0
    cheap_win_model: str = 'gpt-5.6-luna'
    cheap_win_reasoning_effort: str | None = 'high'
    cheap_win_prompt_version: str = 'v4'
    gate_batch_size: int = Field(5, ge=1, le=5)
    win_pre_drop_threshold: float = Field(3.5, ge=0, le=10)
    win_pre_diagnostic_threshold: float = Field(5.5, ge=0, le=10)
    gate_research_event_strength: float = Field(90, ge=0, le=100)
    diagnostic_model: str = 'gpt-5.6-terra'
    diagnostic_reasoning_effort: str | None = 'medium'
    diagnostic_prompt_version: str = 'v3'
    diagnostic_web_search: bool = True
    peer_research_enabled: bool = False
    diagnostic_include_hold: bool = False
    v2_generate_strategy: bool = False

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

    @field_validator('discovery_reasoning_effort', 'scoring_reasoning_effort',
                     'strategy_reasoning_effort', 'fixed_discovery_reasoning_effort',
                     'cheap_win_reasoning_effort', 'diagnostic_reasoning_effort', mode='before')
    @classmethod
    def blank_reasoning_effort_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator('win_pre_diagnostic_threshold')
    @classmethod
    def diagnostic_threshold_is_not_lower_than_drop(cls, value: float,
                                                     info) -> float:
        if value < info.data.get('win_pre_drop_threshold', 0):
            raise ValueError('WIN_PRE_DIAGNOSTIC_THRESHOLD must be >= WIN_PRE_DROP_THRESHOLD')
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
