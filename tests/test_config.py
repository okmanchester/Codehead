from pathlib import Path

import pytest

from codehead.config import Config


def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CODEHEAD_MODEL",
        "CODEHEAD_WORKSPACE",
        "CODEHEAD_SESSION_DB",
        "CODEHEAD_CONTEXT_BUDGET",
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
    assert config.session_db_path == tmp_path / ".codehead" / "sessions.sqlite3"
    assert config.context_budget == 32768


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
    monkeypatch.setenv("CODEHEAD_SESSION_DB", "logs/archive.sqlite3")
    monkeypatch.setenv("CODEHEAD_CONTEXT_BUDGET", "8192")

    config = Config.from_env()

    assert config.model == "custom-model"
    assert config.base_url == "https://example.test"
    assert config.workspace == workspace.resolve()
    assert config.session_db_path == workspace / "logs" / "archive.sqlite3"
    assert config.context_budget == 8192


@pytest.mark.parametrize("budget", ["0", "1023", "-1", "invalid", "1.5"])
def test_invalid_context_budget_is_rejected(tmp_path, monkeypatch, budget):
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("CODEHEAD_CONTEXT_BUDGET", budget)
    with pytest.raises(ValueError):
        Config.from_env()


def test_absolute_session_db_path_is_not_anchored_again(tmp_path):
    archive = tmp_path / "elsewhere" / "archive.sqlite3"
    config = Config(
        api_key="test", workspace=tmp_path / "workspace", session_db=archive
    )
    assert config.session_db_path == archive


def test_config_requires_deepseek_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        Config.from_env()
