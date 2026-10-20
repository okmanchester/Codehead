"""Small artifact helpers shared by the host, controller and task worker."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / ".codehead/evaluations/swebench"
CONTROL_IMAGE = "codehead-swebench-control:5.0.2"
TASK_INSTRUCTIONS = """Fix the issue described below in this repository. Locate the relevant code,
implement a focused repair, and run relevant verification using the project's
existing environment. Report what changed, which checks you actually ran and their
results, and any unresolved limitations. Do not fetch later repository history.

Issue:
"""


def config() -> dict:
    settings = json.loads(Path(__file__).with_name("tasks.json").read_text("utf-8"))
    if settings.get("thinking") not in ("enabled", "disabled"):
        raise ValueError("thinking must be enabled or disabled")
    if settings.get("reasoning_effort") not in ("low", "high", "max"):
        raise ValueError("reasoning_effort must be low, high or max")
    return settings


def driver_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.iterdir(), key=lambda item: item.name):
        if path.is_file() and (
            path.suffix in {".py", ".json", ".txt"} or path.name == "Dockerfile"
        ):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def read_json(path: Path, default=None):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def append_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()


def run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:10]}"
