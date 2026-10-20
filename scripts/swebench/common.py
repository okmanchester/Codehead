"""Small artifact helpers shared by the host, controller and task worker."""

import hashlib
import importlib.util
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


def config(path: Path | None = None) -> dict:
    settings = json.loads(
        (path or Path(__file__).with_name("tasks.json")).read_text("utf-8")
    )
    if settings.get("thinking") not in ("enabled", "disabled"):
        raise ValueError("thinking must be enabled or disabled")
    if settings.get("reasoning_effort") not in ("low", "high", "max"):
        raise ValueError("reasoning_effort must be low, high or max")
    for name in ("max_steps", "max_requests"):
        value = settings.get(name)
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if settings["max_requests"] < settings["max_steps"]:
        raise ValueError(
            "max_requests must cover max_steps, including summary headroom"
        )
    instances = settings.get("instance_ids")
    if not instances or len(instances) != len(set(instances)):
        raise ValueError("instance_ids must be nonempty and unique")
    return settings


def source_fingerprint(package: Path | None = None) -> str:
    """Hash the actual importable core, consistently across checkout/install/OS."""
    if package is None:
        spec = importlib.util.find_spec("codehead")
        if spec is None or spec.origin is None:
            raise RuntimeError("Cannot locate the installed Codehead core")
        package = Path(spec.origin).parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def driver_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.iterdir(), key=lambda item: item.name):
        if path.is_file() and (
            path.suffix in {".py", ".json", ".txt"} or path.name == "Dockerfile"
        ):
            digest.update(path.name.encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
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
