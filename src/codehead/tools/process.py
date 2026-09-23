"""Shared bounded-lifetime process tree, including a pre-execution Windows gate."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from codehead.errors import ToolCleanupError, ToolExecutionError, ToolSkipped


class ManagedProcess:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> None:
        self.cancelled = False
        self.timed_out = False
        self._error: Exception | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._stopped = False
        self._job = None
        self._assigned = False
        if cancel.is_set():
            raise ToolSkipped(
                "Skipped: cancelled before process launch; command did not run"
            )
        process = None
        try:
            options = {}
            if os.name == "nt":
                from codehead.tools._windows_job import WindowsJob

                self._job = WindowsJob()
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("_process_launcher.py")),
                    *command,
                ]
                options = {"creationflags": subprocess.CREATE_NO_WINDOW}
            else:
                options = {"start_new_session": True}
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **options,
            )
            self.process = process
            if self._job is not None:
                self._job.assign(process.pid)
                self._assigned = True
                if cancel.is_set():
                    raise ToolSkipped(
                        "Skipped: cancelled before launch permission; command did not run"
                    )
                process.stdin.write(b"G")
                process.stdin.flush()
            process.stdin.close()
        except BaseException as exc:
            try:
                if process is not None:
                    if self._job is not None and self._assigned:
                        self._job.stop()
                    elif os.name != "nt":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                raise ToolCleanupError(
                    f"Cannot confirm failed launch stopped: {cleanup_error}"
                ) from cleanup_error
            finally:
                try:
                    if process is not None:
                        for pipe in (process.stdin, process.stdout, process.stderr):
                            try:
                                pipe.close()
                            except OSError as close_error:
                                raise ToolCleanupError(
                                    f"Failed to close command pipe: {close_error}"
                                ) from close_error
                finally:
                    if self._job is not None:
                        self._job.close()
            if isinstance(exc, OSError):
                raise ToolExecutionError(f"Failed to own/start command: {exc}") from exc
            raise
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._monitor = threading.Thread(
            target=self._watch, args=(cancel, timeout), daemon=True
        )
        self._monitor.start()

    def _watch(self, cancel: threading.Event, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        try:
            while not self._done.wait(0.02):
                if cancel.is_set() or time.monotonic() >= deadline:
                    self.cancelled = cancel.is_set()
                    self.timed_out = not self.cancelled
                    self.stop()
                    return
                if self.process.poll() is not None:
                    # Background descendants may still own the pipes.
                    self.stop()
                    return
        except Exception as exc:  # noqa: BLE001 - forward watchdog failure to owner
            self._error = exc

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            try:
                if self._job is not None:
                    self._job.stop()
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    else:
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            self.process.poll()
                            try:
                                os.killpg(self.process.pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.02)
                        try:
                            os.killpg(self.process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                self.process.wait(timeout=5)
                self._stopped = True
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ToolCleanupError(
                    f"Cannot confirm process tree stopped: {exc}"
                ) from exc

    def wait(self) -> int:
        while True:
            if self._error is not None:
                raise ToolCleanupError(
                    f"Process cleanup failed: {self._error}"
                ) from self._error
            try:
                return self.process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue

    def close(self) -> None:
        try:
            self.stop()
        finally:
            self._done.set()
            self._monitor.join()
            if self._job is not None:
                self._job.close()
        if self._error is not None:
            raise ToolCleanupError(
                f"Process cleanup failed: {self._error}"
            ) from self._error
