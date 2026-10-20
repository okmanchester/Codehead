"""Immutable, validated configuration values without source loading."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONTEXT_BUDGET = 786432
DEFAULT_MAX_STEPS = 20


class LocalConfig(BaseModel):
    """Workspace and archive location, independent of model credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path
    session_db: Path | None = None

    @field_validator("session_db", mode="before")
    @classmethod
    def nonempty_archive_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("session_db must not be empty")
        return value

    @property
    def session_db_path(self) -> Path:
        path = (self.session_db or Path(".codehead/sessions.sqlite3")).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()


class RuntimeConfig(LocalConfig):
    """The resolved startup snapshot needed to construct a Codehead agent."""

    model: str = Field(default="deepseek-flash", min_length=1)
    api_key: str = Field(min_length=1, repr=False)
    base_url: str = Field(default="https://api.deepseek.com", min_length=1)
    thinking: Literal["enabled", "disabled"] = "disabled"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    context_budget: int = Field(default=DEFAULT_CONTEXT_BUDGET, ge=1024, strict=True)
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, strict=True)

    @field_validator("model", "api_key", "base_url", mode="before")
    @classmethod
    def strip_strings(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value
