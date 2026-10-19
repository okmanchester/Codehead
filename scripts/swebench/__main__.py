"""Windows host entry: python -m scripts.swebench {preflight,validate,run,report}."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.swebench.common import (
    ARTIFACTS,
    CONTROL_IMAGE,
    ROOT,
    config,
    driver_fingerprint,
    read_json,
    run_id,
    save_json,
)


def docker(*args, capture=False, **kwargs):
    return subprocess.run(
        ["docker", *map(str, args)],
        check=True,
        cwd=ROOT,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        **kwargs,
    )


def offline(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "offline-tmp").mkdir(exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_swebench.py",
            "tests/test_openai_provider.py",
            "-q",
            "--tb=short",
            f"--basetemp={root / 'offline-tmp' / run_id('pytest')}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    (root / "offline.log").write_text(result.stdout + result.stderr, "utf-8")
    outcome = {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "driver_sha256": driver_fingerprint(),
    }
    save_json(root / "offline.json", outcome)
    print(result.stdout, end="")
    return outcome["passed"]


def control(command, root, predictions=None):
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    name = run_id("codehead-swebench-control")
    image_id = docker(
        "image", "inspect", CONTROL_IMAGE, "--format", "{{.Id}}", capture=True
    ).stdout.strip()
    arguments = [
        "create",
        "--name",
        name,
        "--label",
        "codehead.swebench=control",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={root},target=/results",
        "--memory",
        "1g",
        "--pids-limit",
        "512",
        "--env",
        f"CODEHEAD_CONTROL_IMAGE={image_id}",
        "--env",
        f"CODEHEAD_DRIVER_SHA256={driver_fingerprint()}",
    ]
    if command == "run":
        if not os.getenv("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        # Pass the name only. Neither the command line nor artifacts contain the key.
        arguments += ["--env", "DEEPSEEK_API_KEY"]
    arguments += [image_id, command]
    if predictions:
        target = root / "replay-predictions.jsonl"
        if predictions.resolve() != target.resolve():
            shutil.copyfile(predictions, target)
        arguments += ["--predictions", "/results/replay-predictions.jsonl"]
    docker(*arguments, capture=True)
    try:
        # The small driver is copied into this trusted container; there is no
        # host repository mount and no gold data in an inference image.
        docker(
            "cp",
            str(ROOT / "scripts/swebench") + "/.",
            f"{name}:/opt/driver/scripts/swebench",
        )
        with (root / f"{command}-control.log").open("w", encoding="utf-8") as log:
            docker("start", "--attach", name, stdout=log, stderr=subprocess.STDOUT)
        exit_code = int(
            docker(
                "inspect", name, "--format", "{{.State.ExitCode}}", capture=True
            ).stdout
        )
        if exit_code:
            print(
                f"Controller failed ({exit_code}); see {root / (command + '-control.log')}"
            )
        return exit_code
    finally:
        docker("rm", "-f", name, capture=True)


def report(root, destination=None):
    preflight = read_json(root / "preflight.json", {})
    settings = preflight.get("settings") or config()
    validation = read_json(root / "validation.json", {})
    offline_result = read_json(root / "offline.json", {})
    analysis = read_json(root / "analysis.json", {})
    rows = []
    for instance in settings["instance_ids"]:
        record = read_json(root / "first-run" / instance / "result.json", {})
        usage = record.get("usage", {})
        review = analysis.get("instances", {}).get(instance, {})
        reason = review.get("reason") or record.get("reason")
        if not record:
            reason = (
                preflight.get("error")
                or validation.get("reason")
                or "尚未运行；原因未确认"
            )
        rows.append(
            {
                "instance_id": instance,
                "official_result": record.get("official_result", "unscorable"),
                "agent_status": record.get("agent_status", "environment_blocked"),
                "requests": usage.get("requests", 0),
                "summary_requests": usage.get("summary_requests", 0),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "missing_usage": usage.get("missing_usage", 0),
                "estimated_cost_cny": usage.get("estimated_cost_cny", 0),
                "inference_seconds": record.get("inference_seconds"),
                "evaluation_seconds": record.get("grading", {}).get(
                    "evaluation_seconds"
                ),
                "reason": reason
                or (
                    "原因未确认"
                    if record.get("official_result") != "resolved"
                    else "已通过官方评分"
                ),
                "patch": record.get("patch"),
                "grading": record.get("grading"),
                "artifacts": f".codehead/evaluations/swebench/first-run/{instance}/",
            }
        )
    outcome = {
        "settings": settings,
        "preflight_ready": preflight.get("ready", False),
        "offline_passed": offline_result.get("passed", False),
        "controls_passed": validation.get("passed", False),
        "instances": rows,
        "resolved_count": sum(r["official_result"] == "resolved" for r in rows),
        "estimated_cost_cny": sum(r["estimated_cost_cny"] for r in rows),
        "analysis": analysis,
    }
    save_json(root / "summary.json", outcome)
    lines = [
        "# SWE-bench Lite 首轮评测",
        "",
        f"数据：`{settings['dataset']}@{settings['revision']}`。模型：`deepseek-flash`，非思考模式。",
        "固定三题、单并发、每题一次；此结果不代表 Lite 整体能力。",
        "",
        f"离线检查：{outcome['offline_passed']}；环境就绪：{outcome['preflight_ready']}；评分对照：{outcome['controls_passed']}。",
        "",
        "| 任务 | 官方结果 | Agent 状态 | 请求 / 摘要 | 输入 / 输出 token | 缺 usage | 估算费用（元） | 推理 / 评分秒 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        durations = " / ".join(
            f"{value:.1f}" if value is not None else "—"
            for value in (row["inference_seconds"], row["evaluation_seconds"])
        )
        lines.append(
            f"| {row['instance_id']} | {row['official_result']} | {row['agent_status']} | "
            f"{row['requests']} / {row['summary_requests']} | {row['input_tokens']} / {row['output_tokens']} | "
            f"{row['missing_usage']} | {row['estimated_cost_cny']:.6f} | {durations} |"
        )
    lines += [
        "",
        f"真实 resolved：{outcome['resolved_count']}/3。累计估算费用：{outcome['estimated_cost_cny']:.6f} 元。",
        "费用按全部输入未命中缓存和高峰价估算，usage 缺失使用保守预留，非服务端账单保证。",
        "",
    ]
    for row in rows:
        lines += [
            f"- `{row['instance_id']}`：{row['reason']}。产物：`{row['artifacts']}`。"
        ]
    for note in analysis.get("observations", []):
        lines += ["", note]
    if analysis.get("next_steps"):
        lines += ["", "下一批待办（按证据和优先级排序）：", ""]
        lines += [f"- {item}" for item in analysis["next_steps"]]
    lines += [
        "",
        "原始证据：`preflight.json`、`offline.log`、`validation.json`、`logs/run_evaluation/`、`first-run/`；均位于 `.codehead/evaluations/swebench/`。",
        "评分可用 `validate --predictions <predictions.jsonl>` 独立重放；每次生成新 run_id。",
        "",
    ]
    path = destination or root / "REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), "utf-8")
    print(path)
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "validate", "run", "report"])
    parser.add_argument("--artifacts", type=Path, default=ARTIFACTS)
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build the trusted control image (preflight)",
    )
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="Validate without Docker/model calls",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        help="Independently replay existing official predictions (validate)",
    )
    parser.add_argument("--output", type=Path, help="Report destination (report)")
    args = parser.parse_args()
    root = args.artifacts.resolve()
    root.mkdir(parents=True, exist_ok=True)
    status = 0
    try:
        if args.command == "report":
            report(root, args.output)
        elif args.command == "preflight":
            free = shutil.disk_usage(root).free
            save_json(
                root / "host.json",
                {"python": sys.version, "free_bytes": free, "artifact_root": str(root)},
            )
            if free < 20 * 1024**3:
                raise RuntimeError(
                    "Less than 20 GiB free on artifact drive; resource check required"
                )
            if args.build:
                docker(
                    "build",
                    "-t",
                    CONTROL_IMAGE,
                    "-f",
                    "scripts/swebench/Dockerfile",
                    ".",
                )
            status = control("preflight", root)
        elif args.command == "validate":
            if not args.predictions and not offline(root):
                status = 1
            elif not args.offline_only:
                status = control("validate", root, args.predictions)
        else:
            status = control("run", root)
            report(root)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        save_json(root / f"host-{args.command}-error.json", {"error": str(exc)})
        print(str(exc), file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()
