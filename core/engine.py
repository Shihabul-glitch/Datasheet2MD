"""App side of the persistent Docling worker (see ``docling_worker.py``).

One worker process at a time, one document at a time. The worker is started
on the first document of a batch, keeps Docling and its models loaded for the
next documents, and is shut down at the end of the batch. It is recycled
(stopped and started fresh) when its memory exceeds a limit after a document,
after a crash, or after a cancellation - cancelling kills the worker's whole
process tree, which is the only reliable way to stop Docling mid-document.
"""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import docling_runner
from .docling_runner import CREATE_NO_WINDOW, DoclingInfo, DoclingRunner, _prepare_frozen_child, child_env

EVENT_PREFIX = "D2MD_EVENT "
START_TIMEOUT_S = 300  # importing Docling + torch from a cold disk can take minutes


def default_recycle_mb() -> int:
    """Restart the worker after a document if it holds more than this (MB)."""
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        total_mb = ms.ullTotalPhys / 2**20
    except Exception:
        total_mb = 8192
    return int(max(1500, min(4096, total_mb * 0.25)))


class EngineError(Exception):
    pass


class EngineCancelled(Exception):
    pass


@dataclass
class EngineResult:
    status: str  # success | partial_success | failure | device_unavailable
    errors: List[str] = field(default_factory=list)
    pages: List[int] = field(default_factory=list)
    page_count: Optional[int] = None
    tables: List[dict] = field(default_factory=list)
    markdown_written: bool = False
    cuda_oom: bool = False
    timings: dict = field(default_factory=dict)
    rss_mb: Optional[int] = None
    worker_started: bool = False  # a new worker (cold start) was needed for this document
    models_loaded_s: Optional[float] = None
    import_s: Optional[float] = None
    log_lines: List[str] = field(default_factory=list)


class WorkerEngine:
    """Persistent worker; ``convert`` blocks (call it from a worker thread)."""

    def __init__(self, info: DoclingInfo, recycle_mb: Optional[int] = None):
        self.info = info
        self.recycle_mb = recycle_mb or default_recycle_mb()
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self.import_s: Optional[float] = None
        self.started_count = 0

    # -------------------------------------------------------------- process
    def _start(self, on_line: Callable[[str], None], on_stage: Callable[[str], None]) -> None:
        on_stage("starting_engine")
        script = docling_runner.worker_script_path()
        if not script.is_file():
            raise EngineError(f"Worker script missing: {script}")
        _prepare_frozen_child()
        self._lines = queue.Queue()
        try:
            proc = subprocess.Popen(
                [self.info.python, "-u", str(script), "--serve"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", bufsize=1, shell=False, env=child_env(),
                cwd=tempfile.gettempdir(),  # never inside an output or staging folder
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise EngineError(f"Could not start Docling's Python: {exc}") from exc
        with self._lock:
            self._proc = proc
        q = self._lines

        def pump():
            assert proc.stdout is not None
            for raw in proc.stdout:
                q.put(raw.rstrip("\n"))
            q.put(None)

        threading.Thread(target=pump, daemon=True, name="worker-stdout").start()
        self.started_count += 1
        ev = self._wait_event({"ready"}, on_line, START_TIMEOUT_S)
        self.import_s = ev.get("import_s")

    def _wait_event(self, names, on_line, timeout: Optional[float] = None) -> dict:
        # The timeout counts time spent polling while awake, not wall-clock time:
        # if Windows sleeps meanwhile, the worker is paused too and must not be
        # declared dead on resume.
        waited = 0.0
        while True:
            if self._cancelled.is_set():
                self.kill()  # make sure the process is gone before the next document starts
                raise EngineCancelled()
            try:
                line = self._lines.get(timeout=0.25)
            except queue.Empty:
                waited += 0.25
                if timeout and waited > timeout:
                    self.kill()
                    raise EngineError(f"Docling's Python did not start within {timeout:.0f} s.")
                continue
            if line is None:
                if self._cancelled.is_set():
                    self.kill()
                    raise EngineCancelled()
                raise EngineError("The Docling worker stopped unexpectedly.")
            if line.startswith(EVENT_PREFIX):
                try:
                    ev = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    continue
                if ev.get("event") in names:
                    return ev
                self._on_other_event(ev)
                continue
            on_line(line)

    def _on_other_event(self, ev: dict) -> None:
        handler = getattr(self, "_stage_cb", None)
        if ev.get("event") == "stage" and handler:
            handler(ev.get("stage", ""))
        elif ev.get("event") == "models_loaded":
            self._models_loaded_s = ev.get("seconds")

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ api
    def convert(self, request: dict, on_line: Callable[[str], None], on_stage: Callable[[str], None]) -> EngineResult:
        self._cancelled.clear()
        self._stage_cb = on_stage
        self._models_loaded_s = None
        started = False
        if not self.alive:
            self._start(on_line, on_stage)
            started = True
        assert self._proc is not None and self._proc.stdin is not None
        lines: List[str] = []

        def collect(line: str):
            lines.append(line)
            on_line(line)

        try:
            self._proc.stdin.write(json.dumps({"op": "convert", **request}) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            self.kill()
            raise EngineError(f"The Docling worker is not responding: {exc}") from exc
        ev = self._wait_event({"done"}, collect)
        res = EngineResult(
            status=ev.get("status", "failure"), errors=ev.get("errors") or [], pages=ev.get("pages") or [],
            page_count=ev.get("page_count"), tables=ev.get("tables") or [],
            markdown_written=bool(ev.get("markdown_written")), cuda_oom=bool(ev.get("cuda_oom")),
            timings=ev.get("timings") or {}, rss_mb=ev.get("rss_mb"), worker_started=started,
            models_loaded_s=self._models_loaded_s, import_s=self.import_s if started else None,
            log_lines=lines[-2000:],
        )
        if res.rss_mb and res.rss_mb > self.recycle_mb:
            on_line(f"[engine] worker holds {res.rss_mb} MB (> {self.recycle_mb} MB); restarting it before the "
                    "next document")
            self.close()
        return res

    def cancel(self) -> None:
        self._cancelled.set()
        self.kill()

    def kill(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            DoclingRunner._kill_tree(proc)
            with self._lock:
                if self._proc is proc:
                    self._proc = None

    def close(self, timeout: float = 15.0) -> None:
        """Ask the worker to exit; kill it if it doesn't."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(json.dumps({"op": "exit"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        self.kill()
        with self._lock:
            self._proc = None
