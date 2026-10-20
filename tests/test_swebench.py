"""Offline evaluation contracts: no Docker, datasets or real model requests."""

import asyncio
import hashlib
import io
import json
import shutil
import sys
import tarfile
from types import SimpleNamespace

import pytest

from codehead.errors import ContextError, ModelError
from codehead.llm.base import LLMProvider
from codehead.models import (
    AssistantMessage,
    FinishReason,
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    TokenUsage,
    ToolCall,
    UserMessage,
)
from codehead.tools import BashTool, ToolRegistry, WriteTool
from scripts.swebench import control
from scripts.swebench.__main__ import report
from scripts.swebench.common import config, read_json
from scripts.swebench.patches import (
    collect_patch,
    environment_members,
    extract_tree,
    git,
    initialize_baseline,
)
from scripts.swebench.provider import BudgetExceeded, MeteredProvider
from scripts.swebench.worker import execute


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_worker_entrypoint_receives_actual_thinking_configuration(
    tmp_path, monkeypatch, thinking
):
    from unittest.mock import Mock

    from scripts.swebench import worker

    cfg = {**config(), "thinking": thinking, "reasoning_effort": "low"}
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "settings": control.worker_settings(cfg),
                "problem_statement": "issue",
                "prior_cost_cny": 0,
            }
        ),
        "utf-8",
    )
    constructor = Mock()
    monkeypatch.setattr(worker, "OpenAIProvider", constructor)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(sys, "argv", ["worker", str(task)])

    async def fake_execute(*args, **kwargs):
        assert args[3]["thinking"] == thinking

    monkeypatch.setattr(worker, "execute", fake_execute)
    worker.main()
    assert constructor.call_args.kwargs["thinking"] == thinking
    assert constructor.call_args.kwargs["reasoning_effort"] == "low"
    assert constructor.call_args.kwargs["max_output_tokens"] == 8192


@pytest.mark.parametrize(
    "driver_sha256,expected_mode",
    [
        (
            "00377f532c7a26bc1dc552236c38d32b8fdfdffd7189ae071ba53f71eed83eee",
            "disabled",
        ),
        (
            "58607c741df4da6fe5f455b24298782e79b7849abd1f2c465e07d5b8f3f94bc6",
            "disabled",
        ),
        (
            "0155ae79154868b28ed2005c104527c9a6d795c02a3e25abc26997a022298f99",
            "disabled",
        ),
        (
            "1c841f5049ae009a921b9371f2814a27139c8d0e062aae37702a60ebdb4234d9",
            "disabled",
        ),
        ("unverified-driver", "unknown"),
    ],
    ids=["e916944-lf", "e916944-crlf", "2b7cb2e-lf", "2b7cb2e-crlf", "unknown"],
)
@pytest.mark.parametrize("old_disable", [False, True, None])
def test_legacy_report_requires_driver_evidence_for_actual_mode(
    tmp_path, driver_sha256, expected_mode, old_disable
):
    saved = config()
    saved.pop("thinking")
    saved.pop("reasoning_effort")
    if old_disable is not None:
        saved["disable_thinking"] = old_disable
    (tmp_path / "preflight.json").write_text(json.dumps({"settings": saved}), "utf-8")
    (tmp_path / "validation.json").write_text(
        json.dumps({"driver_sha256": driver_sha256}),
        "utf-8",
    )
    outcome = report(tmp_path)
    assert outcome["model_configuration"]["thinking"] == expected_mode
    label = expected_mode if expected_mode != "unknown" else "未确认"
    assert f"思考模式：`{label}`" in (tmp_path / "REPORT.md").read_text("utf-8")
    assert outcome["settings"] == saved


def test_report_records_actual_model_mode_effort_and_output_limit(tmp_path):
    saved = {
        **config(),
        "model": "test-model",
        "thinking": "enabled",
        "reasoning_effort": "low",
    }
    (tmp_path / "preflight.json").write_text(json.dumps({"settings": saved}), "utf-8")
    outcome = report(tmp_path)
    assert outcome["model_configuration"] == {
        "model": "test-model",
        "thinking": "enabled",
        "reasoning_effort": "low",
        "max_output_tokens": 8192,
    }
    text = (tmp_path / "REPORT.md").read_text("utf-8")
    assert "test-model" in text and "enabled" in text and "low" in text


@pytest.mark.parametrize(
    "field,value", [("thinking", "auto"), ("reasoning_effort", "medium")]
)
def test_eval_invalid_thinking_settings_fail_before_execution(
    monkeypatch, field, value
):
    settings = {**config(), field: value}
    monkeypatch.setattr("scripts.swebench.common.json.loads", lambda _: settings)
    with pytest.raises(ValueError, match=field):
        config()


@pytest.mark.parametrize(
    "change", [{"thinking": "disabled"}, {"reasoning_effort": "low"}]
)
def test_thinking_setting_change_invalidates_paid_run_validation(
    tmp_path, monkeypatch, change
):
    saved = config()
    fingerprint = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
    (tmp_path / "validation.json").write_text(
        json.dumps(
            {
                "passed": True,
                "settings_sha256": fingerprint,
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    with pytest.raises(RuntimeError, match="fixed configuration"):
        control.run(None, {**saved, **change})
    assert not (tmp_path / "first-run").exists()


class FakeProvider(LLMProvider):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.closed = False

    async def stream(self, request):
        self.requests.append(request)
        reply = next(self.replies)
        if reply == "hang":
            await asyncio.Event().wait()
        if isinstance(reply, Exception):
            raise reply
        yield LLMResponseCompleted(response=reply)

    async def aclose(self):
        self.closed = True


def answer(text="done", *, usage=True):
    return LLMResponse(
        message=AssistantMessage(content=text),
        finish_reason=FinishReason.END_TURN,
        usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        if usage
        else None,
    )


def call(name, arguments):
    return LLMResponse(
        message=AssistantMessage(
            tool_calls=(ToolCall(id="t1", name=name, arguments=arguments),)
        ),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(input_tokens=20, output_tokens=8, total_tokens=28),
    )


@pytest.fixture
def repository(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "tracked.txt").write_text("before\n", "utf-8")
    (baseline / "deleted.txt").write_text("delete me\n", "utf-8")
    (baseline / ".gitignore").write_text("ignored*\n", "utf-8")
    initialize_baseline(baseline)
    changed = tmp_path / "changed"
    shutil.copytree(baseline, changed, ignore=shutil.ignore_patterns(".git"))
    return baseline, changed


@pytest.mark.parametrize(
    "ending,expected",
    [
        ("normal", "completed"),
        ("error", "model_failed"),
        ("steps", "budget_exhausted"),
        ("timeout", "budget_exhausted"),
    ],
)
def test_worker_retains_patch_events_and_usage_after_all_exits(
    tmp_path, repository, ending, expected
):
    baseline, changed = repository
    cfg = config()
    cfg["max_steps"] = 1 if ending == "steps" else 3
    cfg["agent_timeout"] = 0.1 if ending == "timeout" else 5
    first = call("write", {"path": "new.txt", "content": "new content\n"})
    replies = [
        first,
        {
            "normal": answer(),
            "error": ModelError("upstream failure"),
            "steps": answer(),
            "timeout": "hang",
        }[ending],
    ]
    provider = FakeProvider(replies)
    output = tmp_path / "output"
    result = asyncio.run(
        execute(
            changed,
            output,
            "Repair the fixture",
            cfg,
            provider,
            tools=ToolRegistry([WriteTool(changed)]),
        )
    )
    assert result["agent_status"] == expected
    assert provider.closed
    assert (changed / "new.txt").read_text("utf-8") == "new content\n"
    assert read_json(output / "terminal.json")["agent_status"] == expected
    assert read_json(output / "usage.json")["requests"] >= 1
    assert read_json(output / "usage.json")["input_tokens"] >= 20
    events = [
        json.loads(line)
        for line in (output / "events.jsonl").read_text("utf-8").splitlines()
    ]
    assert events[0]["type"] == "turn.started"
    assert any(e["type"] == "tool.completed" for e in events)
    if ending == "normal":
        assert events[-1]["type"] == "turn.completed"
    if ending in {"error", "steps"}:
        assert events[-1]["type"] == "turn.failed"
    patch = tmp_path / "patch.diff"
    assert collect_patch(baseline, changed, patch)["patch_applies"]
    git(baseline, "apply", str(patch))
    assert (baseline / "new.txt").read_bytes() == (changed / "new.txt").read_bytes()
    if ending in {"error", "timeout"}:
        assert result["usage"]["missing_usage"] == 1
        assert result["usage"]["estimated_cost_cny"] > 0.1


def test_patch_add_delete_modify_ignore_and_untrusted_git(tmp_path, repository):
    baseline, changed = repository
    (changed / "tracked.txt").write_bytes(b"after\n")
    (changed / "deleted.txt").unlink()
    (changed / "new.bin").write_bytes(b"\x00binary\xff")
    (changed / "ignored.log").write_text("ignore", "utf-8")
    (changed / ".codehead").mkdir()
    (changed / ".codehead/session.db").write_text("exclude", "utf-8")
    (changed / ".git").mkdir()
    (changed / ".git/HEAD").write_text("malformed", "utf-8")
    patch = tmp_path / "model.patch"
    result = collect_patch(baseline, changed, patch)
    assert result["patch_applies"] and not result["empty_patch"]
    assert b".codehead" not in patch.read_bytes()
    assert b"ignored.log" not in patch.read_bytes()
    git(baseline, "apply", str(patch))
    assert not (baseline / "deleted.txt").exists()
    assert (baseline / "tracked.txt").read_bytes() == b"after\n"
    assert (baseline / "new.bin").read_bytes() == b"\x00binary\xff"


def test_empty_patch_is_explicit(tmp_path, repository):
    baseline, changed = repository
    result = collect_patch(baseline, changed, tmp_path / "empty.patch")
    assert result == {"patch_bytes": 0, "patch_applies": True, "empty_patch": True}


def test_worker_timeout_stops_running_shell_before_return(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    output = tmp_path / "output"
    cfg = config()
    cfg["agent_timeout"] = 3
    script = workspace / "delayed.py"
    script.write_text(
        "import time\nfrom pathlib import Path\nPath('started').touch()\n"
        "time.sleep(4)\nPath('leaked').touch()\n",
        "utf-8",
    )
    command = (
        f'& "{sys.executable}" delayed.py'
        if sys.platform == "win32"
        else f'"{sys.executable}" delayed.py'
    )
    provider = FakeProvider([call("bash", {"command": command})])

    async def check():
        result = await execute(
            workspace,
            output,
            "run",
            cfg,
            provider,
            tools=ToolRegistry([BashTool(workspace)]),
        )
        await asyncio.sleep(4.1)
        return result

    result = asyncio.run(check())
    assert result["agent_status"] == "budget_exhausted"
    assert (workspace / "started").exists()
    assert not (workspace / "leaked").exists()
    assert result["tool_results"][0]["status"] == "cancelled"


def test_summary_count_usage_on_validation_failure_and_total_limit(tmp_path):
    cfg = config()
    cfg["max_requests"] = 2
    # The wrapper counts completed model calls before the core validates text.
    provider = FakeProvider([answer(usage=False), answer()])
    meter = MeteredProvider(provider, cfg, tmp_path)
    request = LLMRequest(messages=(UserMessage(content="summary input"),))

    async def check():
        await meter.complete(request)
        await meter.complete(request)
        with pytest.raises(BudgetExceeded, match="total_request_limit"):
            await meter.complete(request)

    asyncio.run(check())
    usage = read_json(tmp_path / "usage.json")
    assert usage["requests"] == usage["summary_requests"] == 2
    assert usage["missing_usage"] == 1
    assert usage["estimated_cost_cny"] > 0.1
    assert len(provider.requests) == 2


def test_context_capacity_failure_is_not_an_environment_failure(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = FakeProvider([ContextError("History unit exceeds input budget")])
    result = asyncio.run(
        execute(
            workspace,
            tmp_path / "output",
            "issue",
            config(),
            provider,
            tools=ToolRegistry([WriteTool(workspace)]),
        )
    )
    assert result["agent_status"] == "budget_exhausted"
    assert result["usage"]["requests"] == 1


def test_worker_receives_runtime_settings_without_evaluation_metadata(tmp_path):
    cfg = control.worker_settings({**config(), "gold_patch": "unique-answer-marker"})
    assert "unique-answer-marker" not in json.dumps(cfg)
    assert (
        not {"dataset", "revision", "instance_ids", "execution_revision"} & cfg.keys()
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = asyncio.run(
        execute(
            workspace,
            tmp_path / "output",
            "issue",
            cfg,
            FakeProvider([answer()]),
            tools=ToolRegistry([WriteTool(workspace)]),
        )
    )
    assert result["agent_status"] == "completed"


@pytest.mark.parametrize(
    "spent,maximum,reason",
    [(40, 50, "cost_stop_threshold"), (39.9, 40, "next_request_would_exceed_budget")],
)
def test_budget_refuses_before_contacting_provider(tmp_path, spent, maximum, reason):
    cfg = config()
    cfg["max_cost_cny"] = maximum
    provider = FakeProvider([answer()])
    meter = MeteredProvider(provider, cfg, tmp_path, spent=spent)
    with pytest.raises(BudgetExceeded, match=reason):
        asyncio.run(
            meter.complete(LLMRequest(messages=(UserMessage(content="hello"),)))
        )
    assert provider.requests == []


def test_archive_refuses_traversal(tmp_path):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        entry = tarfile.TarInfo("testbed/../../outside")
        archive.addfile(entry)
    with pytest.raises(ValueError, match="Unsafe"):
        extract_tree(data.getvalue(), tmp_path, strip_root=True)


def test_preserve_official_runtime_without_ignored_sources_or_tests():
    names = [
        "astropy/wcs/_wcs.cpython-39-x86_64-linux-gnu.so",
        "astropy/_version.py",
        "astropy.egg-info/PKG-INFO",
        "astropy.egg-info/entry_points.txt",
        "astropy.egg-info/SOURCES.txt",
        "build/lib/astropy/future_source.py",
        "astropy/tests/test_future.py",
        "astropy/tests/helper.so",
        "astropy/__pycache__/x.pyc",
        "astropy/answer.patch",
        "../astropy/escape.so",
        "other_package/module.so",
    ]
    assert environment_members(names, "astropy") == names[:4]
    assert environment_members(
        ["django/conf/locale/de/LC_MESSAGES/django.mo"], "django"
    ) == ["django/conf/locale/de/LC_MESSAGES/django.mo"]


def test_report_reads_artifacts_without_provider_or_docker(tmp_path):
    outcome = report(tmp_path)
    assert len(outcome["instances"]) == 3
    assert outcome["estimated_cost_cny"] == 0
    assert all(r["official_result"] == "unscorable" for r in outcome["instances"])
    assert (tmp_path / "REPORT.md").is_file()


def test_report_preserves_saved_configuration_and_failure_review(tmp_path):
    saved = config()
    saved["max_steps"] = 7
    (tmp_path / "preflight.json").write_text(json.dumps({"settings": saved}), "utf-8")
    (tmp_path / "analysis.json").write_text(
        json.dumps(
            {
                "observations": ["Reviewed evidence"],
                "instances": {
                    saved["instance_ids"][0]: {"reason": "原因未确认；日志不足"}
                },
            }
        ),
        "utf-8",
    )
    outcome = report(tmp_path)
    assert outcome["settings"]["max_steps"] == 7
    assert "Reviewed evidence" in (tmp_path / "REPORT.md").read_text("utf-8")


def test_execution_metadata_must_match_fixed_source_and_answers(tmp_path, monkeypatch):
    cfg = config()
    original = {
        "instance_id": cfg["instance_ids"][0],
        "repo": "example/repo",
        "base_commit": "base",
        "problem_statement": "issue",
        "patch": "gold",
        "test_patch": "tests",
        "version": "1",
        "FAIL_TO_PASS": '["test_repair"]',
        "PASS_TO_PASS": "[]",
    }
    cfg["instance_ids"] = [original["instance_id"]]
    changed = {**original, "patch": "different answer"}
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "datasets",
        SimpleNamespace(
            load_dataset=lambda name, **kwargs: [
                original if name == cfg["dataset"] else changed
            ]
        ),
    )
    with pytest.raises(ValueError, match="/patch"):
        control.load_tasks(cfg)
    assert not (tmp_path / "dataset.json").exists()


@pytest.mark.parametrize("bad_control", ["gold", "empty"])
def test_failed_control_blocks_paid_calls(tmp_path, monkeypatch, bad_control):
    cfg = config()
    rows = [{"instance_id": i, "patch": "gold"} for i in cfg["instance_ids"]]
    (tmp_path / "dataset.json").write_text(json.dumps(rows), "utf-8")
    monkeypatch.setattr(control, "RESULTS", tmp_path)
    monkeypatch.setattr(control, "prepare_runtimes", lambda *args: None)
    monkeypatch.setattr(control, "execution_identity", dict)
    (tmp_path / "preflight.json").write_text(
        json.dumps({"settings": cfg, "execution_identity": {}}), "utf-8"
    )

    def bad_grade(client, row, patch, settings, *, kind, output):
        resolved = kind == "gold"
        if kind == bad_control:
            resolved = not resolved
        return {"resolved": resolved, "run_id": "fake-control"}

    monkeypatch.setattr(control, "grade", bad_grade)
    outcome = control.validate(None, cfg)
    assert outcome["passed"] is False
    # The run guard must refuse before any call to infer, even if offline passed.
    with pytest.raises(RuntimeError, match="controls must all pass"):
        control.run(None, cfg)
    assert not (tmp_path / "first-run").exists()
