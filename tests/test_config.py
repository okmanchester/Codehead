from pathlib import Path

import pytest

from codehead.config import Config


def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CODEHEAD_MODEL",
        "CODEHEAD_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_config_uses_deepseek_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    config = Config.from_env()

    assert config.model == "deepseek-flash"
    assert config.api_key == "secret"
    assert config.base_url == "https://api.deepseek.com"
    assert config.workspace == tmp_path.resolve()


def test_config_accepts_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_config_environment(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test")
    monkeypatch.setenv("CODEHEAD_MODEL", "custom-model")
    monkeypatch.setenv("CODEHEAD_WORKSPACE", str(workspace))

    config = Config.from_env()

    assert config.model == "custom-model"
    assert config.base_url == "https://example.test"
    assert config.workspace == workspace.resolve()


def test_config_requires_deepseek_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        Config.from_env()
