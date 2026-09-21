"""Environment-backed configuration for Codehead."""

import os
from pathlib import Path
from typing import Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


class Config(BaseModel):
    """The minimum configuration needed to construct a Codehead agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    api_key: str | None = None
    base_url: str | None = None
    workspace: Path

    @classmethod
    def from_env(cls) -> Self:
        """Load configuration from a local .env file and the environment."""
        load_dotenv()

        model = _optional_env("CODEHEAD_MODEL")
        if model is None:
            raise ValueError("CODEHEAD_MODEL is required")

        workspace_value = _optional_env("CODEHEAD_WORKSPACE")
        workspace = Path(workspace_value) if workspace_value else Path.cwd()

        return cls(
            model=model,
            api_key=_optional_env("OPENAI_API_KEY"),
            base_url=_optional_env("OPENAI_BASE_URL"),
            workspace=workspace.expanduser().resolve(),
        )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None
