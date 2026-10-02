from pathlib import Path

import pytest
from dotenv import load_dotenv as dotenv_load

from codehead.config import LocalConfig, RuntimeConfig
from codehead.config_loader import load_local_config, load_runtime_config


def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "CODEHEAD_MODEL",
        "CODEHEAD_WORKSPACE",
        "CODEHEAD_SESSION_DB",
        "CODEHEAD_CONTEXT_BUDGET",
        "CODEHEAD_MAX_STEPS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("codehead.config_loader.load_dotenv", lambda: None)


def write_config(workspace: Path, content: str) -> Path:
    path = workspace / ".codehead" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_config_uses_deepseek_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    config = load_runtime_config()

    assert config.model == "deepseek-flash"
    assert config.api_key == "secret"
    assert config.base_url == "https://api.deepseek.com"
    assert config.workspace == tmp_path.resolve()
    assert config.session_db_path == tmp_path / ".codehead" / "sessions.sqlite3"
    assert config.context_budget == 32768
    assert config.max_steps == 20


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
    monkeypatch.setenv("CODEHEAD_MAX_STEPS", "3")

    config = load_runtime_config()

    assert config.model == "custom-model"
    assert config.base_url == "https://example.test"
    assert config.workspace == workspace.resolve()
    assert config.session_db_path == workspace / "logs" / "archive.sqlite3"
    assert config.context_budget == 8192
    assert config.max_steps == 3


@pytest.mark.parametrize("budget", ["0", "1023", "-1", "invalid", "1.5"])
def test_invalid_context_budget_is_rejected(tmp_path, monkeypatch, budget):
    clear_config_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("CODEHEAD_CONTEXT_BUDGET", budget)
    with pytest.raises(ValueError):
        load_runtime_config()


def test_absolute_session_db_path_is_not_anchored_again(tmp_path):
    archive = tmp_path / "elsewhere" / "archive.sqlite3"
    config = RuntimeConfig(
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
        load_runtime_config()


def test_value_objects_do_not_load_sources_and_are_frozen(tmp_path, monkeypatch):
    def unexpected_read():
        raise AssertionError("configuration source read")

    monkeypatch.setattr("codehead.config_loader._read_project_config", unexpected_read)
    monkeypatch.setenv("CODEHEAD_MODEL", "environment-model")
    write_config(tmp_path, 'model = "file-model"')
    config = RuntimeConfig(workspace=tmp_path, api_key="test")
    local = LocalConfig(workspace=tmp_path)
    assert config.model == "deepseek-flash"
    assert local.session_db_path == tmp_path / ".codehead" / "sessions.sqlite3"
    with pytest.raises(ValueError, match="frozen"):
        config.max_steps = 1


def test_toml_configuration_is_found_in_selected_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "nested"
    workspace.mkdir()
    write_config(tmp_path, 'model = "wrong-location"')
    write_config(
        workspace,
        'model = "file-model"\nbase_url = "https://file.test"\n'
        'session_db = "logs/history.sqlite3"\ncontext_budget = 4096\nmax_steps = 4',
    )
    monkeypatch.setenv("CODEHEAD_WORKSPACE", "nested")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

    config = load_runtime_config()
    assert config.workspace == workspace.resolve()
    assert config.model == "file-model"
    assert config.base_url == "https://file.test"
    assert config.session_db_path == workspace / "logs" / "history.sqlite3"
    assert config.context_budget == 4096
    assert config.max_steps == 4


def test_environment_overrides_toml_and_empty_values_do_not(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        'model = "file-model"\nbase_url = "https://file.test"\n'
        'session_db = "file.sqlite3"\ncontext_budget = 4096\nmax_steps = 4',
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("CODEHEAD_MODEL", " env-model ")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "  ")
    monkeypatch.setenv("CODEHEAD_SESSION_DB", "env.sqlite3")
    monkeypatch.setenv("CODEHEAD_CONTEXT_BUDGET", "8192")
    monkeypatch.setenv("CODEHEAD_MAX_STEPS", "")

    config = load_runtime_config()
    assert config.model == "env-model"
    assert config.base_url == "https://file.test"
    assert config.session_db_path == tmp_path / "env.sqlite3"
    assert config.context_budget == 8192
    assert config.max_steps == 4


@pytest.mark.parametrize("process_model", [None, "process-model"])
def test_dotenv_overrides_toml_but_not_process_environment(
    tmp_path, monkeypatch, process_model
):
    write_config(tmp_path, 'model = "toml-model"')
    env_path = tmp_path / ".env"
    env_path.write_text("CODEHEAD_MODEL=dotenv-model\n", encoding="utf-8")
    # Record this variable for cleanup even when dotenv inserts it later.
    monkeypatch.setenv("CODEHEAD_MODEL", "placeholder")
    monkeypatch.delenv("CODEHEAD_MODEL")
    if process_model is not None:
        monkeypatch.setenv("CODEHEAD_MODEL", process_model)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(
        "codehead.config_loader.load_dotenv", lambda: dotenv_load(env_path)
    )

    assert load_runtime_config().model == (process_model or "dotenv-model")


@pytest.mark.parametrize(
    "content",
    [
        'context_budget = "4096"',
        "context_budget = true",
        "context_budget = 1023",
        "max_steps = false",
        'max_steps = "2"',
        "max_steps = 1.5",
        "max_steps = 0",
        'model = "  "',
        "base_url = 7",
        'session_db = ""',
    ],
)
def test_invalid_toml_parameter_types_and_ranges_are_rejected(
    tmp_path, monkeypatch, content
):
    path = write_config(tmp_path, content)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    with pytest.raises(ValueError, match="Invalid runtime configuration") as caught:
        load_runtime_config()
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("name", ["workspace", "api_key", "unknown"])
@pytest.mark.parametrize("loader", [load_runtime_config, load_local_config])
def test_unknown_and_forbidden_file_fields_are_rejected(
    tmp_path, monkeypatch, name, loader
):
    write_config(tmp_path, f'{name} = "value"')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    with pytest.raises(ValueError, match=f"Unknown configuration fields.*{name}"):
        loader()


@pytest.mark.parametrize("loader", [load_runtime_config, load_local_config])
def test_malformed_toml_reports_path_without_falling_back(
    tmp_path, monkeypatch, loader
):
    path = write_config(tmp_path, "model = [")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    with pytest.raises(ValueError, match="Cannot load configuration") as caught:
        loader()
    assert str(path) in str(caught.value)


def test_config_file_read_error_is_reported(tmp_path, monkeypatch):
    path = write_config(tmp_path, "")
    original_open = Path.open

    def denied_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(ValueError, match="Cannot load configuration.*denied"):
        load_local_config()


def test_invalid_utf8_config_reports_file_path(tmp_path):
    path = write_config(tmp_path, "")
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="Cannot load configuration") as caught:
        load_local_config()
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("steps", ["0", "-1", "true", "1.5", "invalid"])
def test_invalid_max_steps_environment_is_rejected(monkeypatch, steps):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("CODEHEAD_MAX_STEPS", steps)
    with pytest.raises(ValueError):
        load_runtime_config()


def test_local_loading_ignores_invalid_model_settings_without_credentials(
    tmp_path, monkeypatch
):
    write_config(
        tmp_path,
        'session_db = "local.sqlite3"\ncontext_budget = "invalid"\n'
        "max_steps = false\nmodel = 7",
    )
    monkeypatch.setenv("CODEHEAD_CONTEXT_BUDGET", "invalid too")
    monkeypatch.setenv("CODEHEAD_MAX_STEPS", "invalid too")
    assert load_local_config().session_db_path == tmp_path / "local.sqlite3"


def test_loaded_config_does_not_change_when_sources_change(tmp_path, monkeypatch):
    path = write_config(tmp_path, 'model = "first"')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    config = load_runtime_config()
    path.write_text('model = "second"', encoding="utf-8")
    monkeypatch.setenv("CODEHEAD_MODEL", "third")
    assert config.model == "first"


def test_missing_workspace_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEHEAD_WORKSPACE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="not a directory"):
        load_local_config()
