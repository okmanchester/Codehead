"""Environment-backed configuration for Codehead."""

import os
from pathlib import Path
from typing import Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


class Config(BaseModel):
    """The minimum configuration needed to construct a Codehead agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = "deepseek-flash"
    api_key: str
    base_url: str = "https://api.deepseek.com"
    workspace: Path

    @classmethod
    def from_env(cls) -> Self:
        """Load configuration from a local .env file and the environment."""
        load_dotenv()

        api_key = _optional_env("DEEPSEEK_API_KEY")
        if api_key is None:
            raise ValueError("DEEPSEEK_API_KEY is required")

        workspace_value = _optional_env("CODEHEAD_WORKSPACE")
        workspace = Path(workspace_value) if workspace_value else Path.cwd()
        resolved_workspace = workspace.expanduser().resolve()
        if not resolved_workspace.is_dir():
            raise ValueError(
                f"CODEHEAD_WORKSPACE is not a directory: {resolved_workspace}"
            )

        return cls(
            model=_optional_env("CODEHEAD_MODEL") or "deepseek-flash",
            api_key=api_key,
            base_url=_optional_env("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com",
            workspace=resolved_workspace,
        )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None
