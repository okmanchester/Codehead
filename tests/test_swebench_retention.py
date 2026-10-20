"""Retention contracts without Docker, datasets or paid model calls."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swebench import __main__ as host
from scripts.swebench import control, retention
from scripts.swebench.common import config, driver_fingerprint, read_json, save_json


def evidence(root, *, count=1, status="completed", official="resolved", manifest=True):
    settings = config()
    settings["instance_ids"] = settings["instance_ids"][:count]
    save_json(root / "preflight.json", {"settings": settings})
    save_json(
        root / "dataset.json", [{"instance_id": i} for i in settings["instance_ids"]]
    )
    runtime = {"passed": True, "instances": {}}
    hashes = {"instances": {}}
    predictions = []
    for instance in settings["instance_ids"]:
        output = root / "first-run" / instance
        patch = "" if official == "empty_patch" else "fixture patch\n"
        for name, data in {
            "model.patch": patch.encode(),
            "agent/events.jsonl": b'{"type":"turn.completed"}\n',
            "agent/session.sqlite3": b"fixture session",
            "work/changed.tar": b"exported complete tree",
        }.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        prediction = {
            "instance_id": instance,
            "model_name_or_path": settings["model"],
            "model_patch": patch,
        }
        predictions.append(prediction)
        score = {"official_result": official, "details": {"infra_failure": False}}
        if official != "empty_patch":
            relative = f"logs/prediction/{instance}/report.json"
            score["report"] = "/results/" + relative
            save_json(root / relative, {instance: {"resolved": official == "resolved"}})
        for name, value in {
            "attempt.json": {"attempt": 1},
            "isolation.json": {"mounts": [], "privileged": False},
            "agent/usage.json": {"requests": 1},
            "agent/terminal.json": {"agent_status": status},
            "grading/prediction.json": prediction,
            "grading/score.json": score,
            "result.json": {
                "agent_status": status,
                "official_result": official,
                "patch_applies": True,
                "pipeline_failed": False,
                "usage": {"requests": 1},
                "acceptance": dict.fromkeys(
                    [
                        "patch_exported",
                        "trace_present",
                        "usage_present",
                        "terminal_present",
                        "graded_or_empty",
                    ],
                    True,
                ),
            },
        }.items():
            save_json(output / name, value)
        prepared = root / "prepared" / (instance + "-fixture")
        prepared.mkdir(parents=True)
        for name in ("source.tar", "environment.tar", "Dockerfile", "build.log"):
            (prepared / name).write_text(name, "utf-8")
        save_json(prepared / "environment-members.json", {"members": []})
        runtime["instances"][instance] = {
            "source_archive": str(prepared / "source.tar")
        }
        hashes["instances"][instance] = {
            "files": {
                str(p.relative_to(output)).replace("\\", "/"): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in output.rglob("*")
                if p.is_file() and p.name != "changed.tar"
            }
        }
    save_json(root / "runtime-validation.json", runtime)
    prediction_file = root / "first-run/predictions.jsonl"
    prediction_file.write_text(
        "".join(json.dumps(p) + "\n" for p in predictions), "utf-8"
    )
    if manifest:
        save_json(root / "first-run/manifest.json", hashes)
    return settings


def snapshot(root):
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "status,official",
    [
        ("completed", "resolved"),
        ("completed", "unresolved"),
        ("budget_exhausted", "resolved"),
        ("model_failed", "unresolved"),
        ("completed", "empty_patch"),
    ],
)
def test_release_only_verified_copies_and_preserve_evidence(tmp_path, status, official):
    evidence(tmp_path, status=status, official=official)
    before = snapshot(tmp_path)
    preview = retention.cleanup(tmp_path)
    assert snapshot(tmp_path) == before
    assert len(preview["candidates"]) == 4
    applied = retention.cleanup(tmp_path, apply=True)
    assert not applied["errors"]
    removed = {Path(item["path"]) for item in applied["removed"]}
    assert removed == {Path(item["path"]) for item in preview["candidates"]}
    for path, data in before.items():
        if path.name == "runtime-validation.json":
            current = json.loads(path.read_text("utf-8"))
            assert current.pop("archives_released") is True
            assert current == json.loads(data)
        elif path in removed:
            assert not path.exists()
        else:
            assert path.read_bytes() == data
    after = snapshot(tmp_path)
    assert not retention.cleanup(tmp_path, apply=True)["removed"]
    assert snapshot(tmp_path) == after


@pytest.mark.parametrize(
    "failure",
    [
        "pipeline",
        "environment",
        "tool",
        "acceptance",
        "events",
        "session",
        "score",
        "report",
        "prediction",
        "manifest",
        "usage-json",
        "dataset",
    ],
)
def test_retain_incomplete_or_failed_infrastructure_evidence(tmp_path, failure):
    settings = evidence(tmp_path)
    output = tmp_path / "first-run" / settings["instance_ids"][0]
    record = read_json(output / "result.json")
    if failure == "pipeline":
        record["pipeline_failed"] = True
    elif failure in {"environment", "tool"}:
        record["agent_status"] = failure + "_failed"
    elif failure == "acceptance":
        record["acceptance"]["trace_present"] = False
    elif failure in {"events", "session", "score"}:
        names = {
            "events": "agent/events.jsonl",
            "session": "agent/session.sqlite3",
            "score": "grading/score.json",
        }
        (output / names[failure]).unlink()
    elif failure == "report":
        next((tmp_path / "logs").rglob("report.json")).unlink()
    elif failure == "prediction":
        save_json(output / "grading/prediction.json", {"model_patch": "different"})
    elif failure == "manifest":
        (output / "agent/events.jsonl").write_text("tampered\n", "utf-8")
    elif failure == "usage-json":
        (output / "agent/usage.json").write_text("{invalid", "utf-8")
    elif failure == "dataset":
        (tmp_path / "dataset.json").unlink()
    save_json(output / "result.json", record)
    before = snapshot(tmp_path)
    outcome = retention.cleanup(tmp_path, apply=True)
    assert not outcome["candidates"]
    assert outcome["retained"]
    assert snapshot(tmp_path) == before


def test_pending_task_keeps_shared_baselines(tmp_path):
    settings = evidence(tmp_path, count=2)
    (tmp_path / "first-run" / settings["instance_ids"][1] / "result.json").unlink()
    outcome = retention.cleanup(tmp_path, apply=True)
    assert len(outcome["removed"]) == 1
    assert len(list((tmp_path / "prepared").rglob("source.tar"))) == 2
    assert "archives_released" not in read_json(tmp_path / "runtime-validation.json")


def test_failed_unlink_is_reported_without_altering_task_result(tmp_path, monkeypatch):
    settings = evidence(tmp_path)
    record = tmp_path / "first-run" / settings["instance_ids"][0] / "result.json"
    before = record.read_bytes()
    unlink = Path.unlink

    def fail_changed(path, *args, **kwargs):
        if path.name == "changed.tar":
            raise PermissionError("fixture locked file")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_changed)
    outcome = retention.cleanup(tmp_path, apply=True)
    assert any("locked file" in error for error in outcome["errors"])
    assert record.read_bytes() == before
    assert "archives_released" not in read_json(tmp_path / "runtime-validation.json")


def test_reject_paths_outside_root_and_links(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.tar").write_text("keep", "utf-8")
    with pytest.raises(ValueError, match="outside"):
        retention.checked_path(root, root / "../outside/source.tar")
    try:
        (root / "prepared").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink privilege unavailable")
    with pytest.raises(ValueError, match="Linked"):
        retention.checked_path(root, root / "prepared/source.tar")
    assert (outside / "source.tar").read_text("utf-8") == "keep"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction contract")
def test_reject_windows_junction_without_symlink_privilege(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "source.tar"
    target.write_text("keep", "utf-8")
    link = root / "prepared"
    quoted_link = "'" + str(link).replace("'", "''") + "'"
    quoted_target = "'" + str(outside).replace("'", "''") + "'"
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Junction -Path {quoted_link} -Target {quoted_target} | Out-Null",
        ],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(ValueError, match="Linked"):
            retention.checked_path(root, link / "source.tar")
        assert target.read_text("utf-8") == "keep"
    finally:
        link.rmdir()


def test_manifest_protected_full_tree_is_never_deleted(tmp_path):
    settings = evidence(tmp_path)
    instance = settings["instance_ids"][0]
    tree = tmp_path / "first-run" / instance / "work/changed.tar"
    path = tmp_path / "first-run/manifest.json"
    manifest = read_json(path)
    manifest["instances"][instance]["files"]["work/changed.tar"] = hashlib.sha256(
        tree.read_bytes()
    ).hexdigest()
    save_json(path, manifest)
    outcome = retention.cleanup(tmp_path, apply=True)
    assert tree.is_file()
    assert len(outcome["removed"]) == 3
    assert any("Manifest protects" in message for message in outcome["retained"])


@pytest.mark.parametrize("released", [True, False])
def test_missing_or_released_baseline_blocks_before_container_or_model(
    tmp_path, monkeypatch, released
):
    settings = evidence(tmp_path)
    path = tmp_path / "runtime-validation.json"
    runtime = read_json(path)
    if released:
        runtime["archives_released"] = True
        save_json(path, runtime)
    else:
        Path(next(iter(runtime["instances"].values()))["source_archive"]).unlink()
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    with pytest.raises(RuntimeError, match="validate again"):
        control.infer(
            None,
            {"instance_id": settings["instance_ids"][0]},
            settings,
            0,
            tmp_path / "new-task",
        )


def test_cleanup_cli_preview_never_creates_root_or_contacts_docker(
    tmp_path, monkeypatch
):
    root = tmp_path / "missing"
    monkeypatch.setattr(sys, "argv", ["swebench", "cleanup", "--artifacts", str(root)])
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(host, "docker", lambda *a, **kw: pytest.fail("Docker called"))
    with pytest.raises(SystemExit) as caught:
        host.main()
    assert caught.value.code == 0
    assert not root.exists()


@pytest.mark.parametrize("success", [True, False])
def test_offline_cleans_only_successful_current_invocation(
    tmp_path, monkeypatch, success
):
    old = tmp_path / "offline-tmp/old"
    old.mkdir(parents=True)
    (old / "keep.txt").write_text("old failure", "utf-8")
    monkeypatch.setattr(host, "run_id", lambda prefix: "fixture-run")

    def run(arguments, **kwargs):
        temporary = Path(
            next(
                arg.split("=", 1)[1]
                for arg in arguments
                if arg.startswith("--basetemp=")
            )
        )
        temporary.mkdir()
        (temporary / "test-output").write_text("fixture", "utf-8")
        return SimpleNamespace(
            returncode=0 if success else 1, stdout="test output", stderr=""
        )

    monkeypatch.setattr(host.subprocess, "run", run)
    assert host.offline(tmp_path) is success
    assert (tmp_path / "offline-tmp/fixture-run").exists() is not success
    assert (old / "keep.txt").read_text("utf-8") == "old failure"
    assert read_json(tmp_path / "offline.json")["passed"] is success
    assert (tmp_path / "offline.log").read_text("utf-8") == "test output"


def test_cleanup_preserves_report_and_prediction_replay(tmp_path, monkeypatch):
    settings = evidence(tmp_path)
    before = host.report(tmp_path)
    retention.cleanup(tmp_path, apply=True)
    assert host.report(tmp_path) == before
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    calls = []

    def grade(client, row, patch, cfg, **kwargs):
        calls.append((row["instance_id"], patch))
        return {"official_result": "resolved"}

    monkeypatch.setattr(control, "grade", grade)
    outcome = control.validate(None, settings, tmp_path / "first-run/predictions.jsonl")
    assert outcome["passed"]
    assert calls == [(settings["instance_ids"][0], "fixture patch\n")]


@pytest.mark.parametrize("locked", [False, True])
def test_run_cleans_after_persisting_acceptance_and_keeps_attempt(
    tmp_path, monkeypatch, locked
):
    settings = evidence(tmp_path, manifest=False)
    instance = settings["instance_ids"][0]
    output = tmp_path / "first-run" / instance
    inference = read_json(output / "result.json")
    score = read_json(output / "grading/score.json")
    (output / "result.json").unlink()
    (output / "attempt.json").unlink()
    identity = {
        "source_sha256": "fixture-source",
        "control_image_id": "fixture-control",
    }
    monkeypatch.setattr(control, "execution_identity", lambda: identity)
    preflight = read_json(tmp_path / "preflight.json")
    preflight.update(
        execution_identity=identity, images={instance: {"id": "fixture-base"}}
    )
    save_json(tmp_path / "preflight.json", preflight)
    runtime = read_json(tmp_path / "runtime-validation.json")
    runtime["execution_identity"] = identity
    runtime["instances"][instance].update(
        base_image_id="fixture-base", image={"id": "fixture-runtime"}
    )
    save_json(tmp_path / "runtime-validation.json", runtime)
    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda image_id: SimpleNamespace(id=image_id))
    )
    save_json(
        tmp_path / "validation.json",
        {
            "passed": True,
            "driver_sha256": driver_fingerprint(),
            "settings_sha256": hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
            ).hexdigest(),
            "execution_identity": identity,
        },
    )
    save_json(
        tmp_path / "offline.json",
        {
            "passed": True,
            "driver_sha256": driver_fingerprint(),
            "source_sha256": "fixture-source",
        },
    )
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    monkeypatch.setattr(control, "infer", lambda *args: inference)
    monkeypatch.setattr(control, "grade", lambda *args, **kwargs: score)
    if locked:
        unlink = Path.unlink

        def fail_changed(path, *args, **kwargs):
            if path.name == "changed.tar":
                raise PermissionError("fixture locked file")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_changed)
    outcome = control.run(client, settings)
    assert outcome["halted"] is None
    assert read_json(output / "result.json")["acceptance"]["patch_exported"]
    assert (output / "work/changed.tar").exists() is locked
    assert (output / "attempt.json").exists()
    monkeypatch.setattr(
        control, "infer", lambda *args: pytest.fail("Repeated inference")
    )
    assert control.run(client, settings)["halted"] is None
