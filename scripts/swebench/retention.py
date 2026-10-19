"""Release verified evaluation copies while retaining replay and diagnosis evidence."""

import hashlib
import os
import re
import shutil
from pathlib import Path

from scripts.swebench.common import read_json, save_json


def checked_path(root: Path, path: Path) -> Path:
    """Check the lexical boundary and links before reading or deleting an artifact."""
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise ValueError(f"Artifact outside root: {path}")
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f"Linked artifact path: {part}")
        if part == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Artifact resolves outside root: {path}")
    return path


def _object(root, path):
    value = read_json(checked_path(root, path), {})
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def _evidence(root, instance, protected):
    output = root / "first-run" / instance
    record = _object(root, output / "result.json")
    acceptance = record.get("acceptance", {})
    if (
        not isinstance(acceptance, dict)
        or record.get("pipeline_failed") is not False
        or record.get("blocked")
        or record.get("patch_applies") is not True
        or record.get("agent_status")
        not in {"completed", "budget_exhausted", "model_failed"}
        or record.get("official_result")
        not in {"resolved", "unresolved", "empty_patch"}
        or not all(
            acceptance.get(key) is True
            for key in (
                "patch_exported",
                "trace_present",
                "usage_present",
                "terminal_present",
                "graded_or_empty",
            )
        )
    ):
        raise ValueError(f"{instance}: result has not passed artifact acceptance")
    for name in (
        "attempt.json",
        "model.patch",
        "isolation.json",
        "agent/events.jsonl",
        "agent/session.sqlite3",
        "agent/usage.json",
        "agent/terminal.json",
        "grading/prediction.json",
        "grading/score.json",
    ):
        if not checked_path(root, output / name).is_file():
            raise ValueError(f"{instance}: missing evidence {name}")
    terminal = _object(root, output / "agent/terminal.json")
    _object(root, output / "agent/usage.json")
    prediction = _object(root, output / "grading/prediction.json")
    score = _object(root, output / "grading/score.json")
    if (
        terminal.get("agent_status") != record["agent_status"]
        or prediction.get("instance_id") != instance
        or prediction.get("model_patch") != (output / "model.patch").read_text("utf-8")
        or score.get("official_result") != record["official_result"]
        or score.get("details", {}).get("infra_failure")
    ):
        raise ValueError(
            f"{instance}: inconsistent terminal, patch or grading evidence"
        )
    if score["official_result"] != "empty_patch":
        report = score.get("report", "")
        path = root / report.removeprefix("/results/")
        details = _object(root, path).get(instance, {}) if report else {}
        if details.get("infra_failure") or details.get("resolved") is not (
            score["official_result"] == "resolved"
        ):
            raise ValueError(f"{instance}: official report missing or inconsistent")
    manifest = _object(root, root / "first-run/manifest.json")
    for name, expected in (
        manifest.get("instances", {}).get(instance, {}).get("files", {}).items()
    ):
        path = checked_path(root, output / name)
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"{instance}: manifest mismatch for {name}")
        protected.add(path)


def cleanup(root: Path, *, apply=False) -> dict:
    """Preview by default; only release copies supported by saved acceptance evidence."""
    root = checked_path(root, root)
    result = {"candidates": [], "removed": [], "retained": [], "errors": []}
    preflight = _object(root, root / "preflight.json")
    instances = preflight.get("settings", {}).get("instance_ids", [])
    if not instances:
        result["retained"].append("No saved fixed task list; nothing released")
        return result
    if any(
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+", name)
        for name in instances
    ):
        raise ValueError("Invalid saved instance identifier")
    dataset = read_json(checked_path(root, root / "dataset.json"), [])
    if (
        not isinstance(dataset, list)
        or [row.get("instance_id") for row in dataset if isinstance(row, dict)]
        != instances
    ):
        result["retained"].append(
            "Fixed dataset missing or inconsistent with saved tasks"
        )
        return result
    accepted = []
    protected = set()
    for instance in instances:
        try:
            _evidence(root, instance, protected)
            accepted.append(instance)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            result["retained"].append(str(exc))

    def candidate(path, reason):
        try:
            path = checked_path(root, path)
            if path in protected:
                result["retained"].append(f"Manifest protects {path}")
            elif path.is_file():
                result["candidates"].append(
                    {"path": str(path), "bytes": path.stat().st_size, "reason": reason}
                )
        except (OSError, ValueError) as exc:
            result["errors"].append(str(exc))

    for instance in accepted:
        candidate(
            root / "first-run" / instance / "work/changed.tar",
            "Task result and replay/diagnosis evidence passed acceptance",
        )
    runtime_path = root / "runtime-validation.json"
    runtime = _object(root, runtime_path)
    release_prepared = len(accepted) == len(instances) and runtime.get("passed") is True
    if release_prepared and not result["errors"]:
        prepared = checked_path(root, root / "prepared")
        if prepared.exists():
            for directory in sorted(prepared.iterdir()):
                if not any(directory.name.startswith(name + "-") for name in instances):
                    continue
                for name in ("source.tar", "environment.tar", "Dockerfile"):
                    candidate(
                        directory / name, "All fixed tasks passed artifact acceptance"
                    )
    if apply:
        for item in result["candidates"]:
            try:
                path = checked_path(root, Path(item["path"]))
                path.unlink()
                result["removed"].append(item)
                if path.name == "changed.tar" and not any(path.parent.iterdir()):
                    path.parent.rmdir()
            except (OSError, ValueError) as exc:
                result["errors"].append(f"{item['path']}: {exc}")
        if (
            release_prepared
            and not result["errors"]
            and not runtime.get("archives_released")
        ):
            runtime["archives_released"] = True
            try:
                save_json(runtime_path, runtime)
            except OSError as exc:
                result["errors"].append(f"Could not mark archives released: {exc}")
    return result


def cleanup_after_run(root: Path) -> None:
    """Cleanup failure never changes a persisted task result or its score."""
    try:
        outcome = cleanup(root, apply=True)
        for message in outcome["errors"]:
            print(f"Cleanup warning: {message}")
    except Exception as exc:  # noqa: BLE001 - cleanup cannot change evaluation outcomes
        print(f"Cleanup warning: {exc}")


def cleanup_offline(root: Path, directory: Path) -> None:
    """Remove only this successful offline invocation's temporary workspace."""
    try:
        root = checked_path(root, root)
        directory = checked_path(root, directory)
        if directory.parent != root / "offline-tmp":
            raise ValueError(f"Not an offline invocation directory: {directory}")
        if directory.exists():
            for path in directory.rglob("*"):
                checked_path(root, path)
            shutil.rmtree(directory)
        if directory.parent.is_dir() and not any(directory.parent.iterdir()):
            directory.parent.rmdir()
    except (OSError, ValueError) as exc:
        print(f"Cleanup warning: {exc}")
