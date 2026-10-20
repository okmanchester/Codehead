"""Trusted Linux controller. Only this container can access Docker and gold data."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import traceback
from pathlib import Path

from scripts.swebench.common import (
    config,
    driver_fingerprint,
    read_json,
    run_id,
    save_json,
)
from scripts.swebench.patches import (
    collect_patch,
    environment_members,
    extract_tree,
    initialize_baseline,
)
from scripts.swebench.projects import PROJECTS
from scripts.swebench.retention import cleanup_after_run

RESULTS = Path("/results")


def execution_identity():
    installed = subprocess.check_output(
        [
            "/opt/agent-venv/bin/python",
            "-c",
            "from scripts.swebench.common import source_fingerprint; print(source_fingerprint())",
        ],
        text=True,
    ).strip()
    if installed != os.environ.get("CODEHEAD_SOURCE_SHA256"):
        raise RuntimeError(
            "Installed Codehead source differs from host; rebuild control image"
        )
    return {
        "source_sha256": installed,
        "control_image_id": os.environ["CODEHEAD_CONTROL_IMAGE"],
    }


def archive_bytes(container, path):
    stream, _ = container.get_archive(path)
    return b"".join(stream)


def file_bytes(container, path):
    with tarfile.open(fileobj=io.BytesIO(archive_bytes(container, path))) as archive:
        return archive.extractfile(
            next(member for member in archive if member.isfile())
        ).read()


def put_files(container, files: dict[str, bytes], destination="/"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    container.put_archive(destination, buffer.getvalue())


def load_tasks(settings):
    """Pin issue/code/answers to the requested original Lite revision.

    v5 requires published execution metadata absent from the old dataset. Borrow
    only those execution fields from a second pinned official dataset and require
    all task identity, source, answers and test lists to agree before using them.
    """
    from datasets import load_dataset

    fixed = list(
        load_dataset(
            settings["dataset"], revision=settings["revision"], split=settings["split"]
        )
    )
    execution = list(
        load_dataset(
            settings["execution_dataset"],
            revision=settings["execution_revision"],
            split=settings["split"],
        )
    )
    fixed = {r["instance_id"]: r for r in fixed}
    execution = {r["instance_id"]: r for r in execution}
    rows = []
    for instance_id in settings["instance_ids"]:
        original = fixed[instance_id]
        supplemental = execution[instance_id]
        for key in (
            "repo",
            "base_commit",
            "problem_statement",
            "patch",
            "test_patch",
            "version",
            "FAIL_TO_PASS",
            "PASS_TO_PASS",
        ):
            left, right = original[key], supplemental[key]
            if key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
                left = json.loads(left) if isinstance(left, str) else left
                right = json.loads(right) if isinstance(right, str) else right
            if left != right:
                raise ValueError(
                    f"Pinned execution metadata differs: {instance_id}/{key}"
                )
        row = dict(original)
        for key in ("image", "eval_script", "log_parser", "eval_type"):
            row[key] = supplemental[key]
        rows.append(row)
    save_json(RESULTS / "dataset.json", rows)
    return rows


def image_record(image):
    return {"id": image.id, "repo_digests": image.attrs.get("RepoDigests", [])}


def project_command(command):
    return [
        "/bin/bash",
        "-c",
        "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && "
        + command,
    ]


def preflight(client, settings):
    from docker.errors import ImageNotFound

    result = {"ready": False, "settings": settings, "checks": [], "images": {}}
    try:
        version = importlib.metadata.version("swebench")
        if version != "5.0.2":
            raise ValueError(f"Unexpected swebench version: {version}")
        info = client.info()
        if info["OSType"] != "linux" or info["Architecture"] not in {"x86_64", "amd64"}:
            raise ValueError("Linux x86_64 Docker engine required")
        if info["MemTotal"] < 6 * 1024**3:
            raise ValueError("Less than 6 GiB Docker memory; resource check required")
        result.update(
            docker={
                k: info[k]
                for k in ("MemTotal", "NCPU", "DockerRootDir", "ServerVersion")
            },
            existing_containers=[c.id for c in client.containers.list()],
            swebench_version=version,
        )
        for lock in ("control", "agent"):
            shutil.copyfile(
                f"/opt/{lock}-requirements.lock", RESULTS / f"{lock}-requirements.lock"
            )
        control = client.images.get(os.environ["CODEHEAD_CONTROL_IMAGE"])
        result["control_image"] = image_record(control)
        result["execution_identity"] = execution_identity()
        result["docker_cli_version"] = subprocess.run(
            ["docker", "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
        rows = load_tasks(settings)
        for row in rows:
            if row["repo"] not in PROJECTS:
                raise ValueError(f"No explicit runtime mapping for {row['repo']}")
            image_name = row["image"]
            try:
                image = client.images.get(image_name)
            except ImageNotFound:
                print(f"Pulling {image_name}", flush=True)
                image = client.images.pull(image_name)
            result["images"][row["instance_id"]] = {
                "name": image_name,
                **image_record(image),
            }
            check = client.containers.run(
                image.id,
                project_command(
                    "python -c 'import sys; print(sys.executable); print(sys.version)' && git --version"
                ),
                entrypoint=[],
                remove=True,
                mem_limit="1g",
                pids_limit=128,
                labels={"codehead.swebench": "preflight"},
            )
            result["checks"].append(
                {
                    "instance_id": row["instance_id"],
                    "project_interpreter": check.decode("utf-8", "replace"),
                }
            )
            save_json(RESULTS / "preflight.json", result)
        result["ready"] = True
    except Exception as exc:  # noqa: BLE001 - persist an environment blocker
        result["error"] = f"{type(exc).__name__}: {exc}"
        (RESULTS / "preflight-error.log").write_text(traceback.format_exc(), "utf-8")
    save_json(RESULTS / "preflight.json", result)
    return result


def grade(
    client,
    row,
    patch: str,
    settings,
    *,
    kind="prediction",
    output: Path,
    model_name=None,
):
    from swebench.harness.constants import RUN_EVALUATION_LOG_DIR
    from swebench.harness.run_evaluation import run_instance
    from swebench.harness.utils import make_test_spec

    row = dict(row)
    row["image"] = read_json(RESULTS / "preflight.json")["images"][row["instance_id"]][
        "id"
    ]

    identifier = run_id(kind)
    prediction = {
        "instance_id": row["instance_id"],
        "model_name_or_path": model_name
        or (settings["model"] if kind == "prediction" else kind),
        "model_patch": patch,
    }
    output.mkdir(parents=True, exist_ok=True)
    save_json(output / "prediction.json", prediction)
    (output / "predictions.jsonl").write_text(json.dumps(prediction) + "\n", "utf-8")
    result = {
        "run_id": identifier,
        "kind": kind,
        "official_result": "unscorable",
        "resolved": None,
        "empty_patch": not bool(patch),
        "prediction": str(output / "prediction.json"),
    }
    if not patch and kind == "prediction":
        result.update(
            official_result="empty_patch", reason="Official CLI skips empty predictions"
        )
        save_json(output / "score.json", result)
        return result
    started = time.monotonic()
    try:
        print(f"Grading {row['instance_id']} ({kind}, {identifier})", flush=True)
        # Call the official primitive for all grades. Its negative-test switch
        # actually runs the baseline tests instead of the CLI's empty-patch skip.
        run_instance(
            make_test_spec(row),
            prediction,
            client,
            identifier,
            timeout=settings["evaluation_timeout"],
            skip_patch=kind == "empty",
        )
        log_dir = (
            RESULTS
            / RUN_EVALUATION_LOG_DIR
            / identifier
            / prediction["model_name_or_path"].replace("/", "__")
            / row["instance_id"]
        )
        report_path = log_dir / "report.json"
        result["log_dir"] = str(log_dir)
        report = read_json(report_path)
        if report is not None:
            details = report[row["instance_id"]]
            result.update(
                resolved=details["resolved"],
                official_result="resolved" if details["resolved"] else "unresolved",
                report=str(report_path),
                details=details,
            )
        else:
            result["reason"] = (
                "Official Harness did not produce report.json; inspect run_instance.log"
            )
    except Exception as exc:  # noqa: BLE001 - preserve an unscorable result
        result["reason"] = f"{type(exc).__name__}: {exc}"
        (output / "grade-error.log").write_text(traceback.format_exc(), "utf-8")
    result["evaluation_seconds"] = time.monotonic() - started
    save_json(output / "score.json", result)
    return result


def validate(client, settings, predictions=None):
    rows = read_json(RESULTS / "dataset.json")
    if predictions:
        rows = {r["instance_id"]: r for r in rows}
        scores = []
        for line in Path(predictions).read_text("utf-8").splitlines():
            pred = json.loads(line)
            row = rows[pred["instance_id"]]
            scores.append(
                grade(
                    client,
                    row,
                    pred["model_patch"],
                    settings,
                    output=RESULTS / "replays" / run_id(row["instance_id"]),
                    model_name=pred["model_name_or_path"],
                )
            )
        return {
            "replayed": True,
            "scores": scores,
            "passed": all(s["official_result"] != "unscorable" for s in scores),
        }
    validation = {"passed": False, "scores": []}
    save_json(RESULTS / "validation.json", validation)
    try:
        identity = execution_identity()
        preflight = read_json(RESULTS / "preflight.json", {})
        if (
            preflight.get("settings") != settings
            or preflight.get("execution_identity") != identity
        ):
            raise RuntimeError("Preflight configuration or execution identity changed")
        prepare_runtimes(client, rows)
    except Exception as exc:  # noqa: BLE001 - no paid call before runtime validation
        validation["reason"] = f"Runtime validation failed: {type(exc).__name__}: {exc}"
        (RESULTS / "runtime-error.log").write_text(traceback.format_exc(), "utf-8")
        save_json(RESULTS / "validation.json", validation)
        return validation
    for index, row in enumerate(rows):
        for kind in ["gold", "gold", "empty"] if index == 0 else ["gold", "empty"]:
            score = grade(
                client,
                row,
                row["patch"] if kind == "gold" else "",
                settings,
                kind=kind,
                output=RESULTS / "validation" / run_id(row["instance_id"] + "-" + kind),
            )
            validation["scores"].append({"instance_id": row["instance_id"], **score})
            save_json(RESULTS / "validation.json", validation)
            expected = kind == "gold"
            if score["resolved"] is not expected or score.get("details", {}).get(
                "infra_failure"
            ):
                validation["reason"] = f"{row['instance_id']} {kind} control failed"
                save_json(RESULTS / "validation.json", validation)
                return validation
    validation["passed"] = True
    validation["driver_sha256"] = driver_fingerprint()
    validation["execution_identity"] = identity
    validation["settings_sha256"] = hashlib.sha256(
        json.dumps(settings, sort_keys=True).encode()
    ).hexdigest()
    save_json(RESULTS / "validation.json", validation)
    return validation


def prepare_runtimes(client, rows):
    preflight = read_json(RESULTS / "preflight.json")
    result = {
        "passed": False,
        "instances": {},
        "execution_identity": preflight["execution_identity"],
    }
    save_json(RESULTS / "runtime-validation.json", result)
    probe = (
        "import sys,subprocess,pathlib; "
        "from codehead.llm import OpenAIProvider; from codehead.tools import BashTool; "
        "from scripts.swebench.common import source_fingerprint; "
        "from scripts.swebench.shell_checks import verify_shell; "
        "print(sys.executable); print(sys.version); "
        "assert sys.version_info[:2] == (3,12); "
        "assert sys.prefix == '/opt/agent-venv'; "
        "assert not pathlib.Path('/var/run/docker.sock').exists(); "
        "assert subprocess.check_output(['git','rev-list','--count','HEAD']).strip() == b'1'; "
        "assert not subprocess.check_output(['git','remote']).strip(); "
        f"assert source_fingerprint() == {preflight['execution_identity']['source_sha256']!r}; "
        "print(verify_shell(pathlib.Path('/testbed')))"
    )
    for row in rows:
        instance_id = row["instance_id"]
        print(f"Preparing isolated runtime: {instance_id}", flush=True)
        work = RESULTS / "prepared" / run_id(instance_id)
        work.mkdir(parents=True)
        source = source_archive(
            client, row, preflight["images"][instance_id]["id"], work
        )
        image = inference_image(
            client, preflight["images"][instance_id]["id"], source, work
        )
        module = PROJECTS[row["repo"]][0]
        output = client.containers.run(
            image.id,
            project_command(
                "python -c "
                + shlex.quote(f"import {module}; print({module}.__file__)")
                + " && /opt/agent-tools/rg --version && "
                "/opt/agent-venv/bin/python -c " + shlex.quote(probe)
            ),
            entrypoint=[],
            remove=True,
            mem_limit="1g",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={"codehead.swebench": "runtime-check"},
        )
        result["instances"][instance_id] = {
            "image": image_record(image),
            "base_image_id": preflight["images"][instance_id]["id"],
            "source_archive": str(work / "source.tar"),
            "environment_archive": str(work / "environment.tar"),
            "environment_members": read_json(work / "environment-members.json"),
            "probe": output.decode("utf-8", "replace"),
        }
        save_json(RESULTS / "runtime-validation.json", result)
    result["passed"] = True
    save_json(RESULTS / "runtime-validation.json", result)


def source_archive(client, row, image_id, work):
    container = client.containers.create(
        image_id,
        ["tail", "-f", "/dev/null"],
        entrypoint=[],
        labels={"codehead.swebench": "source"},
    )
    try:
        container.start()
        # git archive normally honors export-ignore/export-subst. Override them
        # only in this disposable source container to preserve every base file.
        put_files(
            container,
            {"testbed/.git/info/attributes": b"* -export-ignore -export-subst\n"},
        )
        code, output = container.exec_run(
            [
                "git",
                "-C",
                "/testbed",
                "archive",
                "--format=tar",
                "--output=/tmp/codehead-base.tar",
                row["base_commit"],
            ]
        )
        if code:
            raise RuntimeError(output.decode("utf-8", "replace"))
        data = file_bytes(container, "/tmp/codehead-base.tar")
        (work / "source.tar").write_bytes(data)
        code, ignored = container.exec_run(
            [
                "git",
                "-C",
                "/testbed",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ]
        )
        if code:
            raise RuntimeError(ignored.decode("utf-8", "replace"))
        module, packages, metadata = PROJECTS[row["repo"]]
        members = environment_members(
            ignored.decode("utf-8").split("\0"),
            module,
            package_paths=packages,
            metadata_paths=metadata,
        )
        save_json(work / "environment-members.json", members)
        put_files(
            container,
            {
                "tmp/environment-files": b"".join(
                    name.encode() + b"\0" for name in members
                )
            },
        )
        code, output = container.exec_run(
            [
                "tar",
                "-C",
                "/testbed",
                "-cf",
                "/tmp/codehead-environment.tar",
                "--null",
                "-T",
                "/tmp/environment-files",
            ]
        )
        if code:
            raise RuntimeError(output.decode("utf-8", "replace"))
        (work / "environment.tar").write_bytes(
            file_bytes(container, "/tmp/codehead-environment.tar")
        )
        return data
    finally:
        container.remove(force=True)


def inference_image(client, image_id, source: bytes, work: Path):
    controller_image = os.environ["CODEHEAD_CONTROL_IMAGE"]
    dockerfile = f"""FROM {controller_image} AS runtime
FROM {image_id}
USER root
RUN rm -rf /testbed /root /tmp/* /setup_repo.sh /setup_env.sh /eval.sh /patch.diff && mkdir -p /testbed /output /input
COPY repository/ /testbed/
COPY --from=runtime /opt/agent-python /opt/agent-python
COPY --from=runtime /opt/agent-venv /opt/agent-venv
COPY --from=runtime /opt/agent-tools /opt/agent-tools
COPY driver/ /opt/driver/
RUN cd /testbed && git init -q && git add -f . && git -c user.name=Evaluation -c user.email=evaluation@localhost commit -qm 'Local baseline'
COPY environment/ /testbed/
WORKDIR /testbed
ENV PYTHONPATH=/opt/driver
ENTRYPOINT []
"""
    (work / "Dockerfile").write_text(dockerfile, "utf-8")
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode="w") as archive:
        member = tarfile.TarInfo("Dockerfile")
        data = dockerfile.encode()
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
        for prefix, contents in (
            ("repository", source),
            ("environment", (work / "environment.tar").read_bytes()),
        ):
            directory = tarfile.TarInfo(prefix)
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(directory)
            with tarfile.open(fileobj=io.BytesIO(contents)) as repo:
                for entry in repo.getmembers():
                    entry.name = prefix + "/" + entry.name
                    archive.addfile(
                        entry, repo.extractfile(entry) if entry.isfile() else None
                    )
        for name in (
            "__init__.py",
            "swebench/__init__.py",
            "swebench/common.py",
            "swebench/provider.py",
            "swebench/worker.py",
            "swebench/shell_checks.py",
        ):
            archive.add(
                Path("/opt/driver/scripts") / name, arcname="driver/scripts/" + name
            )
    context.seek(0)
    tag = "codehead-swebench-agent:" + work.name
    with (work / "build.log").open("w", encoding="utf-8") as log:
        for event in client.api.build(
            fileobj=context, custom_context=True, tag=tag, rm=True, decode=True
        ):
            log.write(json.dumps(event) + "\n")
            log.flush()
            if "error" in event:
                raise RuntimeError(event["error"])
    return client.images.get(tag)


def worker_settings(settings):
    """Do not deliver dataset IDs/revisions or future evaluation metadata."""
    return {
        name: settings[name]
        for name in (
            "model",
            "base_url",
            "thinking",
            "reasoning_effort",
            "context_budget",
            "max_steps",
            "max_requests",
            "max_output_tokens",
            "agent_timeout",
            "shell_timeout",
            "stop_cost_cny",
            "max_cost_cny",
            "input_cny_per_million",
            "output_cny_per_million",
        )
    }


def infer(client, row, settings, spent, output):
    runtime = read_json(RESULTS / "runtime-validation.json")
    if not runtime["passed"]:
        raise RuntimeError("Task runtimes have not passed validation")
    prepared = runtime["instances"][row["instance_id"]]
    if (
        runtime.get("archives_released")
        or not Path(prepared["source_archive"]).is_file()
    ):
        raise RuntimeError(
            "Runtime archives released or missing; validate again before inference"
        )
    work = output / "work"
    work.mkdir(parents=True)
    image = client.images.get(prepared["image"]["id"])
    task = {
        "problem_statement": row["problem_statement"],
        "settings": worker_settings(settings),
        "prior_cost_cny": spent,
    }
    container = client.containers.create(
        image.id,
        project_command(
            "export PATH=/opt/agent-tools:$PATH; exec /opt/agent-venv/bin/python -m scripts.swebench.worker /input/task.json"
        ),
        entrypoint=[],
        environment={"DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"]},
        mem_limit="4g",
        nano_cpus=4_000_000_000,
        pids_limit=256,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        labels={"codehead.swebench": "inference", "instance_id": row["instance_id"]},
    )
    started = time.monotonic()
    hard_timeout = False
    result = {}
    try:
        container.reload()
        inspect = container.attrs
        # No socket, no workspace mount, no privileged mode, no inherited volumes.
        if inspect["Mounts"] or inspect["HostConfig"]["Privileged"]:
            raise RuntimeError("Inference isolation check failed")
        save_json(
            output / "isolation.json",
            {
                "container_id": container.id,
                "mounts": inspect["Mounts"],
                "privileged": False,
                "image": image_record(image),
                "source_base_commit": row["base_commit"],
                "settings": settings,
            },
        )
        put_files(container, {"input/task.json": json.dumps(task).encode()})
        container.start()
        while True:
            container.reload()
            if container.status in {"exited", "dead"}:
                break
            # Allow existing loop/tool cancellation to settle, then enforce a
            # container boundary if the Python process cannot finish cleanup.
            if time.monotonic() - started > settings["agent_timeout"] + 30:
                hard_timeout = True
                container.kill()
                container.wait(timeout=30)
                break
            time.sleep(1)
        (output / "worker.log").write_bytes(container.logs())
        extract_tree(
            archive_bytes(container, "/output"), output / "agent", strip_root=True
        )
        result = read_json(
            output / "agent/terminal.json",
            {
                "agent_status": "budget_exhausted"
                if hard_timeout
                else "environment_failed",
                "reason": "supervisor_timeout"
                if hard_timeout
                else "worker exited without terminal artifact",
            },
        )
        if "usage" not in result:
            result["usage"] = read_json(output / "agent/usage.json", {})
        changed_tree = archive_bytes(container, "/testbed")
        (work / "changed.tar").write_bytes(changed_tree)
        # Git and both trees stay on a Linux filesystem. Windows artifact mounts
        # can lose executable bits; tar archives preserve those bits for replay.
        with tempfile.TemporaryDirectory(prefix="codehead-patch-") as directory:
            baseline, changed = (
                Path(directory) / "baseline",
                Path(directory) / "changed",
            )
            extract_tree(Path(prepared["source_archive"]).read_bytes(), baseline)
            initialize_baseline(baseline)
            extract_tree(changed_tree, changed, strip_root=True)
            result.update(collect_patch(baseline, changed, output / "model.patch"))
        result["supervisor_seconds"] = time.monotonic() - started
        result["container_exit_code"] = container.attrs["State"]["ExitCode"]
        return result
    finally:
        # Only remove our own task container. Stopped files have already been
        # copied; a collection error retains this container for diagnosis.
        container.reload()
        if container.status == "running":
            container.kill()
            container.wait(timeout=30)
        if not (output / "agent/usage.json").exists():
            extract_tree(
                archive_bytes(container, "/output"), output / "agent", strip_root=True
            )
        if result.get("patch_applies"):
            container.remove(force=True)


def run(client, settings):
    validation = read_json(RESULTS / "validation.json", {})
    fingerprint = hashlib.sha256(
        json.dumps(settings, sort_keys=True).encode()
    ).hexdigest()
    if not validation.get("passed") or validation.get("settings_sha256") != fingerprint:
        raise RuntimeError(
            "Gold and negative controls must all pass for this fixed configuration"
        )
    offline = read_json(RESULTS / "offline.json", {})
    if not offline.get("passed"):
        raise RuntimeError("Offline validation must pass before paid calls")
    if any(
        check.get("driver_sha256") != driver_fingerprint()
        for check in (offline, validation)
    ):
        raise RuntimeError(
            "Evaluation driver changed after validation; validate again before paid calls"
        )
    identity = execution_identity()
    preflight = read_json(RESULTS / "preflight.json", {})
    runtime = read_json(RESULTS / "runtime-validation.json", {})
    if (
        offline.get("source_sha256") != identity["source_sha256"]
        or any(
            record.get("execution_identity") != identity
            for record in (preflight, validation, runtime)
        )
        or preflight.get("settings") != settings
        or runtime.get("passed") is not True
    ):
        raise RuntimeError(
            "Source, image or runtime validation changed; validate again before paid calls"
        )
    rows = read_json(RESULTS / "dataset.json")
    if [row["instance_id"] for row in rows] != settings["instance_ids"]:
        raise RuntimeError("Saved dataset does not match frozen task order")
    for row in rows:
        instance = row["instance_id"]
        prepared = runtime["instances"][instance]
        if prepared["base_image_id"] != preflight["images"][instance]["id"]:
            raise RuntimeError("Prepared runtime base image differs from preflight")
        if client.images.get(prepared["image"]["id"]).id != prepared["image"]["id"]:
            raise RuntimeError("Prepared inference image changed")
    run_dir = RESULTS / "first-run"
    run_dir.mkdir(exist_ok=True)
    spent = 0.0
    halted = None
    records = []
    # Exactly one attempt per fixed instance, even across repeated invocations.
    for row in rows:
        output = run_dir / row["instance_id"]
        record_path = output / "result.json"
        previous = read_json(record_path)
        if previous is not None:
            records.append(previous)
            spent += previous.get("usage", {}).get("estimated_cost_cny", 0)
            if previous.get("pipeline_failed"):
                halted = previous.get("reason", "previous pipeline failure")
            cleanup_after_run(RESULTS)
            continue
        output.mkdir(exist_ok=True)
        record = {
            "instance_id": row["instance_id"],
            "agent_status": "environment_failed",
            "official_result": "unscorable",
            "pipeline_failed": False,
        }
        if halted or spent >= settings["stop_cost_cny"]:
            record.update(
                reason=halted or "global_cost_stop_threshold",
                blocked=True,
                agent_status="environment_failed" if halted else "budget_exhausted",
            )
        elif (output / "attempt.json").exists():
            record.update(
                reason="An interrupted attempt already exists; inspect artifacts, do not retry",
                blocked=True,
                pipeline_failed=True,
            )
            halted = record["reason"]
        else:
            save_json(output / "attempt.json", {"attempt": 1, "settings": settings})
            try:
                record.update(infer(client, row, settings, spent, output))
                spent += record.get("usage", {}).get("estimated_cost_cny", 0)
                patch = (output / "model.patch").read_text("utf-8")
                score = grade(client, row, patch, settings, output=output / "grading")
                record.update(
                    official_result=score["official_result"],
                    grading=score,
                    patch=str(output / "model.patch"),
                )
                # An ordinary unsuccessful repair can continue; failures of
                # isolation/export/grading or tool infrastructure stop the run.
                if score["official_result"] == "unscorable" or record[
                    "agent_status"
                ] in {"environment_failed", "tool_failed"}:
                    record["pipeline_failed"] = True
                    halted = "Inference or scoring infrastructure failure"
                record["acceptance"] = {
                    "patch_exported": record["patch_applies"],
                    "trace_present": (output / "agent/events.jsonl").exists(),
                    "usage_present": (output / "agent/usage.json").exists(),
                    "terminal_present": (output / "agent/terminal.json").exists(),
                    "graded_or_empty": score["official_result"] != "unscorable",
                }
                if not all(record["acceptance"].values()):
                    record["pipeline_failed"] = True
                    halted = "Single-instance acceptance failed"
            except Exception as exc:  # noqa: BLE001 - stop later paid tasks
                terminal = read_json(output / "agent/terminal.json", {})
                if terminal:
                    record.update(terminal)
                usage = read_json(output / "agent/usage.json")
                if usage is not None:
                    record["usage"] = usage
                halted = f"{type(exc).__name__}: {exc}"
                record.update(
                    reason=halted,
                    pipeline_failed=True,
                    agent_status="environment_failed",
                )
                (output / "pipeline-error.log").write_text(
                    traceback.format_exc(), "utf-8"
                )
        save_json(record_path, record)
        records.append(record)
        cleanup_after_run(RESULTS)
    save_json(run_dir / "results.json", records)
    return {
        "records": records,
        "estimated_cost_cny": sum(
            r.get("usage", {}).get("estimated_cost_cny", 0) for r in records
        ),
        "halted": halted,
    }


def main():
    import docker

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["preflight", "validate", "run"])
    parser.add_argument("--predictions")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    client = docker.from_env(timeout=1800)
    settings = config(args.config)
    try:
        if os.environ.get("CODEHEAD_DRIVER_SHA256") != driver_fingerprint():
            raise RuntimeError("Host and control driver fingerprints differ")
        if args.command == "preflight":
            result = preflight(client, settings)
        elif args.command == "validate":
            if not read_json(RESULTS / "preflight.json", {}).get("ready"):
                raise RuntimeError("preflight must pass first")
            result = validate(client, settings, args.predictions)
        else:
            result = run(client, settings)
        save_json(RESULTS / f"{args.command}-outcome.json", result)
        if (
            result.get("ready") is False
            or result.get("passed") is False
            or result.get("halted")
        ):
            raise SystemExit(1)
    except Exception as exc:
        save_json(
            RESULTS / f"{args.command}-error.json",
            {"error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
