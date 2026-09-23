"""Environment-backed configuration for Codehead."""

import os
from pathlib import Path
from typing import Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


class LocalConfig(BaseModel):
    """Workspace and archive location, independent of model credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path
    session_db: Path | None = None

    @property
    def session_db_path(self) -> Path:
        path = (self.session_db or Path(".codehead/sessions.sqlite3")).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    @classmethod
    def from_env(cls) -> Self:
        """Resolve local paths without requiring model or tool configuration."""
        load_dotenv()

        workspace_value = _optional_env("CODEHEAD_WORKSPACE")
        workspace = Path(workspace_value) if workspace_value else Path.cwd()
        resolved_workspace = workspace.expanduser().resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(
                f"CODEHEAD_WORKSPACE is not a directory: {resolved_workspace}"
            )

        return cls(
            workspace=resolved_workspace,
            session_db=_optional_env("CODEHEAD_SESSION_DB"),
        )


class Config(LocalConfig):
    """The configuration needed to construct a Codehead agent."""

    model: str = "deepseek-flash"
    api_key: str
    base_url: str = "https://api.deepseek.com"
    context_budget: int = Field(default=32768, ge=1024)

    @classmethod
    def from_env(cls) -> Self:
        local = LocalConfig.from_env()
        api_key = _optional_env("DEEPSEEK_API_KEY")
        if api_key is None:
            raise ValueError("DEEPSEEK_API_KEY is required")
        return cls(
            model=_optional_env("CODEHEAD_MODEL") or "deepseek-flash",
            api_key=api_key,
            base_url=_optional_env("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            workspace=local.workspace,
            session_db=local.session_db,
            context_budget=_optional_env("CODEHEAD_CONTEXT_BUDGET") or 32768,
        )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None
