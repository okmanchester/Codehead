"""Real process-tree cleanup on the host platform."""

import os
import sys
import threading

import pytest

from codehead.errors import ToolCancelled, ToolExecutionError, ToolSkipped
from codehead.tools.bash import BashTool
from codehead.tools.process import ManagedProcess
from codehead.tools.search import GlobTool


def test_cancel_before_launch_runs_no_command(tmp_path):
    marker = tmp_path / "marker"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ToolSkipped):
        ManagedProcess(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
            cwd=tmp_path,
            cancel=cancel,
            timeout=10,
        )
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object launch gate")
def test_windows_job_binding_failure_does_not_run_command(tmp_path, monkeypatch):
    from codehead.tools import _windows_job

    marker = tmp_path / "marker"

    class FailedJob:
        def assign(self, pid):
            raise OSError("injected assignment failure")

        def close(self):
            pass

    monkeypatch.setattr(_windows_job, "WindowsJob", FailedJob)
    with pytest.raises(ToolExecutionError, match="assignment failure"):
        ManagedProcess(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout=10,
        )
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows launch permission")
def test_windows_cancel_before_launch_permission_does_not_run_command(
    tmp_path, monkeypatch
):
    from codehead.tools import _windows_job

    marker = tmp_path / "marker"
    assigned = threading.Event()
    release = threading.Event()
    original = _windows_job.WindowsJob

    class PausedJob(original):
        def assign(self, pid):
            original.assign(self, pid)
            assigned.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(_windows_job, "WindowsJob", PausedJob)
    cancel = threading.Event()
    failures = []

    def launch():
        try:
            ManagedProcess(
                [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                cwd=tmp_path,
                cancel=cancel,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - inspected below
            failures.append(exc)

    worker = threading.Thread(target=launch)
    worker.start()
    assert assigned.wait(timeout=5)
    cancel.set()
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], ToolSkipped)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process tree")
def test_windows_cancel_reaps_child_and_grandchild(tmp_path):
    import ctypes
    from ctypes import wintypes as w

    child_script = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    parent_script = (
        "import signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]); "
        "print(child.pid,flush=True); "
        "exec(\"while True:\\n print('output'*1000,flush=True)\\n time.sleep(.01)\")"
    )
    cancel = threading.Event()
    owned = ManagedProcess(
        [sys.executable, "-c", parent_script], cwd=tmp_path, cancel=cancel, timeout=15
    )
    try:
        pid_line = owned.stdout.readline()
        assert pid_line
        grandchild_pid = int(pid_line)
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        api.WaitForSingleObject.restype = w.DWORD
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x00100000, False, grandchild_pid)
        assert handle
        try:
            cancel.set()
            owned.wait()
            owned.close()
            assert owned.cancelled
            assert api.WaitForSingleObject(handle, 0) == 0
        finally:
            api.CloseHandle(handle)
    finally:
        owned.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix process group")
def test_unix_cancel_reaps_process_group(tmp_path):
    parent_script = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(child.pid,flush=True); time.sleep(30)"
    )
    cancel = threading.Event()
    owned = ManagedProcess(
        [sys.executable, "-c", parent_script], cwd=tmp_path, cancel=cancel, timeout=15
    )
    try:
        grandchild_pid = int(owned.stdout.readline())
        cancel.set()
        owned.wait()
        owned.close()
        assert owned.cancelled
        with pytest.raises(ProcessLookupError):
            os.killpg(owned.process.pid, 0)
        assert grandchild_pid > 0
    finally:
        owned.close()


def test_search_cancellation_retains_partial_output(tmp_path, monkeypatch):
    marker = tmp_path / "ready"
    script = (
        "import pathlib,sys,time; "
        "sys.stdout.buffer.write(b'file.txt\\0'); sys.stdout.buffer.flush(); "
        f"pathlib.Path({str(marker)!r}).write_text('ready'); "
        "time.sleep(30)"
    )
    real_process = ManagedProcess

    def fake_search_process(command, **kwargs):
        return real_process([sys.executable, "-u", "-c", script], **kwargs)

    monkeypatch.setattr("codehead.tools.search.ManagedProcess", fake_search_process)
    tool = GlobTool(tmp_path, rg_executable="unused")
    cancel = threading.Event()
    outcome = []

    def search():
        try:
            tool.execute_cancellable({"pattern": "*"}, cancel)
        except ToolCancelled as exc:
            outcome.append(str(exc))

    worker = threading.Thread(target=search)
    worker.start()
    deadline = threading.Event()
    for _ in range(500):
        if marker.exists():
            break
        deadline.wait(0.01)
    assert marker.exists()
    cancel.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert "partial side effects" in outcome[0]


def test_bash_cancellation_retains_bounded_partial_output(tmp_path, monkeypatch):
    marker = tmp_path / "ready"
    script = (
        "import pathlib,sys,time; "
        "sys.stdout.write('started\\n'); sys.stdout.flush(); "
        f"pathlib.Path({str(marker)!r}).write_text('ready'); "
        "time.sleep(30)"
    )
    monkeypatch.setattr(
        "codehead.tools.bash._shell_command",
        lambda command: [sys.executable, "-u", "-c", script],
    )
    cancel = threading.Event()
    outcome = []

    def run():
        try:
            BashTool(tmp_path, timeout_seconds=15).execute_cancellable(
                {"command": "ignored"}, cancel
            )
        except ToolCancelled as exc:
            outcome.append(str(exc))

    worker = threading.Thread(target=run)
    worker.start()
    deadline = threading.Event()
    for _ in range(500):
        if marker.exists():
            break
        deadline.wait(0.01)
    assert marker.exists()
    cancel.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert "started" in outcome[0]
    assert "partial side effects" in outcome[0]
