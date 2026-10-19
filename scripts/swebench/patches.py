"""Collect a stopped task's tree against a trusted, external Git baseline."""

import io
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def environment_members(names: list[str], module: str) -> list[str]:
    """Retain runtime binaries and install metadata, never ignored source/tests."""
    metadata = {
        "PKG-INFO",
        "top_level.txt",
        "requires.txt",
        "entry_points.txt",
        "dependency_links.txt",
    }
    selected = []
    for name in names:
        path = PurePosixPath(name)
        parts = path.parts
        if not parts or path.is_absolute() or ".." in parts or "tests" in parts:
            continue
        if parts[0] == module:
            if re.search(r"\.(?:so(?:\.\d+)*|pyd|dll|dylib|mo)$", path.name) or (
                len(parts) == 2 and path.name == "_version.py"
            ):
                selected.append(name)
        elif (
            len(parts) == 2
            and parts[0].lower() == f"{module.lower()}.egg-info"
            and path.name in metadata
        ):
            selected.append(name)
    return selected


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def extract_tree(data: bytes, destination: Path, *, strip_root=False) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if strip_root:
                path = PurePosixPath(*path.parts[1:])
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not path.parts or path.parts[0] in {".git", ".codehead"}:
                continue
            member.name = path.as_posix()
            archive.extract(member, destination, filter="data")


def initialize_baseline(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.name", "Codehead evaluation")
    git(root, "config", "user.email", "evaluation@localhost")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-f", ".")
    git(root, "commit", "-qm", "Local baseline")
    (root / ".git/info/exclude").write_text(".codehead/\n", "utf-8")


def collect_patch(baseline: Path, changed: Path, destination: Path) -> dict:
    command = ["git", f"--git-dir={baseline / '.git'}", f"--work-tree={changed}"]
    # Trust only the controller's index, never the task's .git or its HEAD.
    subprocess.run(
        command + ["add", "--intent-to-add", "--all"],
        cwd=changed,
        check=True,
        capture_output=True,
    )
    patch = subprocess.run(
        command + ["diff", "--binary", "HEAD", "--", ".", ":!.codehead"],
        cwd=changed,
        check=True,
        capture_output=True,
    ).stdout
    destination.write_bytes(patch)
    if patch:
        git(baseline, "apply", "--check", str(destination.resolve()))
    return {
        "patch_bytes": len(patch),
        "patch_applies": True,
        "empty_patch": not bool(patch),
    }
