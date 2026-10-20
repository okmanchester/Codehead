"""Load one startup snapshot from project TOML and environment overrides."""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from codehead.config import LocalConfig, RuntimeConfig

_ENV_FIELDS = {
    "model": "CODEHEAD_MODEL",
    "base_url": "DEEPSEEK_BASE_URL",
    "thinking": "CODEHEAD_THINKING",
    "reasoning_effort": "CODEHEAD_REASONING_EFFORT",
    "session_db": "CODEHEAD_SESSION_DB",
    "context_budget": "CODEHEAD_CONTEXT_BUDGET",
    "max_steps": "CODEHEAD_MAX_STEPS",
}
_INTEGER_FIELDS = {"context_budget", "max_steps"}


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip() or None


def _read_project_config() -> tuple[Path, Path, dict[str, object]]:
    load_dotenv()
    workspace_value = _optional_env("CODEHEAD_WORKSPACE")
    try:
        workspace = Path(workspace_value) if workspace_value else Path.cwd()
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"CODEHEAD_WORKSPACE is not a directory: {workspace}")
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot resolve workspace: {exc}") from exc
    path = workspace / ".codehead" / "config.toml"
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    except FileNotFoundError:
        values = {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot load configuration from {path}: {exc}") from exc
    unknown = values.keys() - _ENV_FIELDS.keys()
    if unknown:
        raise ValueError(
            f"Unknown configuration fields in {path}: {', '.join(sorted(unknown))}"
        )
    return workspace, path, values


def _with_environment(
    values: dict[str, object], fields: tuple[str, ...]
) -> dict[str, object]:
    merged = {field: values[field] for field in fields if field in values}
    for field in fields:
        name = _ENV_FIELDS[field]
        value = _optional_env(name)
        if value is None:
            continue
        if field in _INTEGER_FIELDS:
            try:
                merged[field] = int(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc
        else:
            merged[field] = value
    return merged


def load_local_config() -> LocalConfig:
    """Load archive paths without requiring credentials or model settings."""
    workspace, path, values = _read_project_config()
    merged = _with_environment(values, ("session_db",))
    try:
        return LocalConfig(workspace=workspace, **merged)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid local configuration from {path} or environment: {exc}"
        ) from exc


def load_runtime_config() -> RuntimeConfig:
    """Load and validate all settings once, before constructing runtime resources."""
    workspace, path, values = _read_project_config()
    merged = _with_environment(values, tuple(_ENV_FIELDS))
    api_key = _optional_env("DEEPSEEK_API_KEY")
    if api_key is None:
        raise ValueError("DEEPSEEK_API_KEY is required")
    try:
        return RuntimeConfig(workspace=workspace, api_key=api_key, **merged)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid runtime configuration from {path} or environment: {exc}"
        ) from exc
