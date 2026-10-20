import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swebench import control
from scripts.swebench.__main__ import control as host_control
from scripts.swebench.__main__ import report
from scripts.swebench.common import (
    config,
    driver_fingerprint,
    save_json,
    source_fingerprint,
)
from scripts.swebench.patches import environment_members
from scripts.swebench.projects import PROJECTS
from scripts.swebench.selection import select_tasks


def test_source_fingerprint_tracks_core_and_normalizes_line_endings(tmp_path):
    path = tmp_path / "core.py"
    path.write_bytes(b"value = 1\r\n")
    before = source_fingerprint(tmp_path)
    path.write_bytes(b"value = 1\n")
    assert source_fingerprint(tmp_path) == before
    path.write_bytes(b"value = 2\n")
    assert source_fingerprint(tmp_path) != before


def test_stratified_selection_is_order_independent_and_excludes_debug_tasks():
    rows = [
        {"repo": repo, "instance_id": f"{repo}__task-{index}"}
        for repo, size in (("a", 9), ("b", 5), ("c", 2))
        for index in range(size)
    ]
    selected, metadata = select_tasks(rows, {"a__task-0"}, count=8)
    assert select_tasks(list(reversed(rows)), {"a__task-0"}, count=8) == (
        selected,
        metadata,
    )
    assert len(selected) == len(set(selected)) == 8
    assert "a__task-0" not in selected
    assert metadata["quotas"] == {"a": 4, "b": 3, "c": 1}
    assert [item.split("__")[0] for item in selected[:3]] == ["a", "b", "c"]


def test_frozen_twenty_task_configuration():
    original = config()
    frozen = config(Path("scripts/swebench/tasks-20.json"))
    assert (original["max_steps"], original["max_requests"]) == (100, 125)
    assert (original["thinking"], original["reasoning_effort"]) == ("enabled", "high")
    assert (frozen["max_steps"], frozen["max_requests"]) == (100, 125)
    assert len(set(frozen["instance_ids"])) == 20
    assert not set(original["instance_ids"]) & set(frozen["instance_ids"])
    assert len(frozen["selection"]["quotas"]) == len(PROJECTS) == 12
    assert sum(frozen["selection"]["quotas"].values()) == 20
    for key in (
        "model",
        "thinking",
        "context_budget",
        "max_output_tokens",
        "agent_timeout",
        "shell_timeout",
        "stop_cost_cny",
        "max_cost_cny",
    ):
        assert frozen[key] == original[key]


def test_runtime_artifacts_support_src_layout_and_different_distribution_name():
    names = [
        "lib/matplotlib/_path.so",
        "lib/matplotlib/_version.py",
        "lib/matplotlib.egg-info/PKG-INFO",
        "lib/matplotlib/tests/answer.so",
        "lib/matplotlib/ignored_source.py",
        "build/lib/matplotlib/_path.so",
    ]
    module, packages, metadata = PROJECTS["matplotlib/matplotlib"]
    assert (
        environment_members(
            names, module, package_paths=packages, metadata_paths=metadata
        )
        == names[:3]
    )
    assert PROJECTS["scikit-learn/scikit-learn"][0] == "sklearn"


def test_host_forwards_identical_config_and_refuses_directory_reuse(
    tmp_path, monkeypatch
):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            stdout="sha256:test\n" if args[:2] == ("image", "inspect") else "0\n"
        )

    monkeypatch.setattr("scripts.swebench.__main__.docker", docker)
    monkeypatch.setattr("dotenv.load_dotenv", lambda _: None)
    custom = tmp_path / "custom.json"
    settings = config()
    settings["thinking"] = "disabled"
    save_json(custom, settings)
    assert host_control("preflight", tmp_path, config_path=custom) == 0
    assert json.loads((tmp_path / "config.json").read_text("utf-8")) == settings
    created = next(args for args in calls if args[0] == "create")
    assert created[-3:] == ("preflight", "--config", "/results/config.json")
    assert any(str(arg).startswith("CODEHEAD_SOURCE_SHA256=") for arg in created)
    with pytest.raises(ValueError, match="different frozen configuration"):
        host_control("preflight", tmp_path)


@pytest.mark.parametrize(
    "changed", ["offline_source", "control_image", "runtime_source"]
)
def test_execution_identity_change_blocks_before_paid_inference(
    tmp_path, monkeypatch, changed
):
    settings = config()
    identity = {"source_sha256": "source", "control_image_id": "image"}
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    monkeypatch.setattr(control, "execution_identity", lambda: identity)
    save_json(
        tmp_path / "validation.json",
        {
            "passed": True,
            "driver_sha256": driver_fingerprint(),
            "settings_sha256": hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
            ).hexdigest(),
            "execution_identity": dict(identity),
        },
    )
    save_json(
        tmp_path / "preflight.json",
        {"settings": settings, "execution_identity": dict(identity)},
    )
    save_json(
        tmp_path / "runtime-validation.json",
        {"passed": True, "execution_identity": dict(identity)},
    )
    save_json(
        tmp_path / "offline.json",
        {
            "passed": True,
            "driver_sha256": driver_fingerprint(),
            "source_sha256": "changed" if changed == "offline_source" else "source",
        },
    )
    if changed == "control_image":
        identity["control_image_id"] = "replacement"
    elif changed == "runtime_source":
        save_json(
            tmp_path / "runtime-validation.json",
            {"passed": True, "execution_identity": {}},
        )
    with pytest.raises(RuntimeError, match="Source, image or runtime"):
        control.run(None, settings)
    assert not (tmp_path / "first-run").exists()


def test_installed_source_mismatch_requires_image_rebuild(monkeypatch):
    monkeypatch.setenv("CODEHEAD_SOURCE_SHA256", "new")
    monkeypatch.setattr(
        control.subprocess, "check_output", lambda *args, **kwargs: "old\n"
    )
    with pytest.raises(RuntimeError, match="rebuild control image"):
        control.execution_identity()


def test_report_uses_saved_twenty_task_count_budget_and_artifact_root(tmp_path):
    frozen = config(Path("scripts/swebench/tasks-20.json"))
    save_json(tmp_path / "config.json", frozen)
    result = report(tmp_path)
    text = (tmp_path / "REPORT.md").read_text("utf-8")
    assert len(result["instances"]) == result["not_run_count"] == 20
    assert "0/20" in text and "100" in text and "125" in text
    assert str(tmp_path) in text
    assert "固定三题" not in text
