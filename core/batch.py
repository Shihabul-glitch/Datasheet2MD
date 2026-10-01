"""Queue, sequential batch runner and batch summary (shared by GUI and CLI).

* **Convert** converts every file in the list that is not completed yet
  (pending, failed, cancelled, interrupted, incomplete), one at a time.
* **Cancel Current** kills the running conversion (only the app's own worker
  process tree); that file becomes *Cancelled* and the next file starts.
* **Stop Batch** cancels the running conversion and starts nothing else.
* A failed file never stops the batch (unless ``stop_on_error``, CLI only).

One document at a time: Docling's models need 1.5-2.5 GB of RAM per process,
so parallel conversions would mostly cause memory pressure on typical PCs.
The Docling worker (and its loaded models) is reused for all files of a batch
and shut down when the batch ends.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .config import APP_NAME, APP_VERSION, STAGING_DIRNAME
from .converter import (
    CANCELLED,
    COMPLETED,
    FAILED,
    PARTIAL,
    WARNINGS,
    CliEngine,
    ConversionRequest,
    ConversionResult,
    Converter,
    UiEvent,
)
from .docling_runner import DoclingInfo
from .engine import WorkerEngine
from .markdown_cleaner import CleanupOptions
from .output_manager import OutputError, Reservations, plan_destination

PENDING = "pending"
RUNNING = "running"
INTERRUPTED = "interrupted"

STATUS_LABELS = {
    PENDING: "Pending",
    RUNNING: "Running",
    COMPLETED: "Completed",
    WARNINGS: "Completed (warnings)",
    PARTIAL: "Incomplete",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
    INTERRUPTED: "Interrupted",
}
DONE = (COMPLETED, WARNINGS)
QUEUE_FILE_VERSION = 2


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def path_key(path: os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.path.realpath(str(path))))


@dataclass
class Job:
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    size: Optional[int] = None
    status: str = PENDING
    stage: str = ""
    added_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    elapsed_s: Optional[float] = None
    output_dir: Optional[str] = None
    markdown_path: Optional[str] = None
    log_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    error: str = ""
    error_title: str = ""
    pages: Optional[int] = None
    timings: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.name:
            self.name = Path(self.source).name

    @property
    def status_label(self) -> str:
        label = STATUS_LABELS.get(self.status, self.status)
        if self.status == WARNINGS:
            label = f"Completed ({len(self.warnings)} warning{'s' if len(self.warnings) != 1 else ''})"
        return label

    @property
    def runnable(self) -> bool:
        return self.status not in DONE and self.status != RUNNING

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class AddResult:
    added: List[Job] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)
    not_pdf: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def message(self) -> str:
        parts = [f"Added {len(self.added)} PDF(s)"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} already in the list")
        if self.not_pdf:
            parts.append(f"{len(self.not_pdf)} not a PDF")
        if self.missing:
            parts.append(f"{len(self.missing)} not found")
        return "; ".join(parts) + "."


def iter_pdfs(folder: Path, recursive: bool) -> List[Path]:
    """PDF files in ``folder`` (sorted, case-insensitive .pdf), skipping app staging folders."""
    found: List[Path] = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d != STAGING_DIRNAME)
            found += [Path(root) / f for f in sorted(files) if f.lower().endswith(".pdf")]
    else:
        try:
            found = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        except OSError:
            return []
    return found


class BatchQueue:
    """Ordered, thread-safe list of jobs with atomic persistence."""

    def __init__(self, state_path: Optional[Path] = None):
        self.jobs: List[Job] = []
        self.state_path = state_path
        self._lock = threading.RLock()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def snapshot(self) -> List[Job]:
        with self._lock:
            return [Job.from_dict(j.to_dict()) for j in self.jobs]

    def next_runnable(self, skip: Iterable[str] = ()) -> Optional[Job]:
        skip = set(skip)
        with self._lock:
            return next((j for j in self.jobs if j.runnable and j.id not in skip), None)

    def add_paths(self, paths: Iterable[os.PathLike], recursive: bool = False) -> AddResult:
        res = AddResult()
        candidates: List[Path] = []
        for raw in paths:
            p = Path(str(raw).strip().strip('"')).expanduser()
            if p.is_dir():
                pdfs = iter_pdfs(p, recursive)
                if not pdfs:
                    res.not_pdf.append(f"{p} (folder without PDFs)")
                candidates += pdfs
            elif p.is_file():
                if p.suffix.lower() == ".pdf":
                    candidates.append(p)
                else:
                    res.not_pdf.append(str(p))
            else:
                res.missing.append(str(p))
        with self._lock:
            existing = {path_key(j.source) for j in self.jobs}
            for pdf in candidates:
                key = path_key(pdf)
                if key in existing:
                    res.duplicates.append(str(pdf))
                    continue
                try:
                    size = pdf.stat().st_size
                except OSError:
                    size = None
                job = Job(source=str(pdf.resolve()), size=size)
                self.jobs.append(job)
                existing.add(key)
                res.added.append(job)
        return res

    def remove(self, job_ids: Iterable[str]) -> int:
        ids = set(job_ids)
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if not (j.id in ids and j.status != RUNNING)]
            return before - len(self.jobs)

    def clear(self) -> int:
        """Remove everything except a running file (output folders are never touched)."""
        with self._lock:
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if j.status == RUNNING]
            return before - len(self.jobs)

    # ---------------------------------------------------------- persistence
    def save(self) -> None:
        if not self.state_path:
            return
        with self._lock:
            data = {"version": QUEUE_FILE_VERSION, "app": f"{APP_NAME} {APP_VERSION}", "saved": _now(),
                    "jobs": [j.to_dict() for j in self.jobs]}
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.state_path)
            except OSError:
                pass  # persistence is best effort; never break a conversion over it

    @classmethod
    def load(cls, state_path: Path) -> "BatchQueue":
        """Restore a saved list. A file that was running becomes Interrupted;
        nothing is started automatically. Unknown/old fields are ignored."""
        q = cls(state_path)
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return q
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return q
        for raw in data["jobs"]:
            if not isinstance(raw, dict) or not raw.get("source"):
                continue
            try:
                job = Job.from_dict(raw)
            except TypeError:
                continue
            if job.status == RUNNING:
                job.status, job.stage = INTERRUPTED, ""
                job.error = job.error or "The application closed while this file was being converted."
            elif job.status not in STATUS_LABELS:  # statuses of removed features (e.g. v2.0 "skipped")
                job.status = PENDING if job.status != "skipped" else COMPLETED
            q.jobs.append(job)
        return q


@dataclass
class BatchOptions:
    output_root: Optional[Path] = None  # None: beside each source PDF
    ocr: bool = False
    device: str = "cpu"  # resolved: cpu | cuda
    llm_cleanup: bool = True
    cleanup_options: CleanupOptions = field(default_factory=CleanupOptions)
    debug: bool = False
    artifacts_setting: str = "auto"
    page_range: Optional[str] = None
    num_threads: Optional[int] = None
    recycle_mb: Optional[int] = None
    stop_on_error: bool = False


IDLE, RUNNING_STATE, STOPPING = "idle", "running", "stopping"


class BatchRunner:
    """Runs the queue's unfinished jobs sequentially on a background thread.

    ``emit(kind, payload)`` is called from the worker thread with
    ``("job", job_dict)``, ``("log", (job_id, UiEvent))``, ``("state", state)``,
    ``("done", summary_dict)``. GUI code must marshal these to the Tk thread.
    """

    def __init__(self, queue: BatchQueue, info: DoclingInfo, options: BatchOptions,
                 emit: Optional[Callable[[str, object], None]] = None, engine=None):
        self.queue = queue
        self.info = info
        self.options = options
        self._emit = emit or (lambda kind, payload: None)
        self.reservations = Reservations()
        self.engine = engine
        self._thread: Optional[threading.Thread] = None
        self._converter: Optional[Converter] = None
        self._current: Optional[Job] = None
        self._stop = threading.Event()
        self._state = IDLE
        self.processed: List[str] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_job_id(self) -> Optional[str]:
        return self._current.id if self._current else None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="batch-runner", daemon=True)
        self._thread.start()

    def cancel_current(self) -> None:
        conv = self._converter
        if conv:
            threading.Thread(target=conv.cancel, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._set_state(STOPPING)
        self.cancel_current()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _set_state(self, state: str) -> None:
        self._state = state
        self._emit("state", state)

    def _job_changed(self, job: Job) -> None:
        self.queue.save()
        self._emit("job", job.to_dict())

    def _make_engine(self):
        if self.info.backend == "worker" and self.info.python:
            return WorkerEngine(self.info, self.options.recycle_mb)
        return CliEngine(self.info)

    def run(self) -> dict:
        """Blocking run over all unfinished jobs; returns the batch summary."""
        started = time.monotonic()
        self.processed = []
        self._set_state(RUNNING_STATE)
        if self.engine is None:
            self.engine = self._make_engine()
        # Reserve every destination first: same-named PDFs can't claim the same folder.
        plans = {}
        for job in [j for j in self.queue.jobs if j.runnable]:
            plans[job.id] = self._plan(job)
        keep_system_awake(True)  # set on this (batch) thread; cleared below on the same thread
        try:
            while not self._stop.is_set():
                job = self.queue.next_runnable(skip=self.processed)
                if job is None:
                    break
                plan = plans.pop(job.id, None) or self._plan(job)
                self._run_job(job, plan)
                if self.options.stop_on_error and job.status in (FAILED, PARTIAL):
                    self._emit("log", (job.id, UiEvent("log", "Stopping after a failure (--stop-on-error).",
                                                       "warning")))
                    break
        finally:
            keep_system_awake(False)
            self._current = None
            self._converter = None
            try:
                self.engine.close()  # end of batch: the worker and its models are released
            except Exception:
                pass
            self.reservations.clear()
            self.queue.save()
            summary = batch_summary(self.queue.snapshot(), self.processed, time.monotonic() - started)
            self._set_state(IDLE)
            self._emit("done", summary)
        return summary

    def _plan(self, job: Job):
        try:
            return plan_destination(job.source, self.options.output_root, self.reservations, job.id)
        except OutputError as exc:
            return exc
        except OSError as exc:
            return OutputError(f"Output location unavailable: {exc}")

    def _run_job(self, job: Job, plan) -> None:
        self._current = job
        self.processed.append(job.id)
        job.status, job.stage = RUNNING, "Preparing"
        job.started_at, job.finished_at, job.elapsed_s = _now(), None, None
        job.error = job.error_title = ""
        job.warnings, job.timings = [], {}
        self._job_changed(job)
        started = time.monotonic()
        if isinstance(plan, Exception):
            result = ConversionResult(status=FAILED, error_title="Output folder problem", error_message=str(plan))
        else:
            o = self.options
            request = ConversionRequest(
                pdf_path=Path(job.source), output_root=o.output_root, ocr=o.ocr, device=o.device,
                llm_cleanup=o.llm_cleanup, cleanup_options=o.cleanup_options, debug=o.debug,
                artifacts_setting=o.artifacts_setting, page_range=o.page_range, num_threads=o.num_threads,
                job_id=job.id, plan=plan, reservations=self.reservations)

            def emit(ev: UiEvent, job=job):
                if ev.kind == "stage":
                    job.stage = ev.text
                    self._emit("job", job.to_dict())
                self._emit("log", (job.id, ev))

            self._converter = Converter(request, self.engine, self.info, emit)
            if self._stop.is_set():
                self._converter.cancel()
            try:
                result = self._converter.run()
            except Exception as exc:  # Converter.run catches everything; this is a safety net
                result = ConversionResult(status=FAILED, error_title="Conversion failed", error_message=str(exc))
            self._converter = None
        job.status = result.status
        job.stage = ""
        job.finished_at = _now()
        job.elapsed_s = round(time.monotonic() - started, 1)
        job.output_dir = str(result.output_dir) if result.output_dir else None
        job.markdown_path = str(result.markdown_path) if result.markdown_path else None
        job.log_path = str(result.log_path) if result.log_path else None
        job.warnings = list(dict.fromkeys(result.warnings))[:50]
        job.error, job.error_title = result.error_message or "", result.error_title or ""
        job.pages = result.page_count
        job.timings = dict(result.timings)
        self._current = None
        self._job_changed(job)


def keep_system_awake(enable: bool) -> None:
    """Ask Windows not to idle-sleep while a batch runs (closing the lid still sleeps)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0))
    except Exception:
        pass


def batch_summary(jobs: List[Job], processed_ids: Optional[List[str]] = None, seconds: float = 0.0) -> dict:
    rows = [j for j in jobs if processed_ids is None or j.id in processed_ids]
    totals: Dict[str, int] = {}
    for j in rows:
        totals[j.status] = totals.get(j.status, 0) + 1
    return {
        "generated_by": f"{APP_NAME} {APP_VERSION}",
        "generated_at": _now(),
        "duration_s": round(seconds, 1),
        "files": len(rows),
        "totals": totals,
        "succeeded": sum(totals.get(s, 0) for s in DONE),
        "failed": sum(totals.get(s, 0) for s in (FAILED, PARTIAL)),
        "cancelled": totals.get(CANCELLED, 0) + totals.get(INTERRUPTED, 0),
        "pending": totals.get(PENDING, 0),
        "jobs": [{"source": j.source, "status": j.status, "pages": j.pages, "duration_s": j.elapsed_s,
                  "output_dir": j.output_dir, "markdown": j.markdown_path, "warnings": j.warnings,
                  "error": j.error, "timings": j.timings, "log": j.log_path} for j in rows],
    }
