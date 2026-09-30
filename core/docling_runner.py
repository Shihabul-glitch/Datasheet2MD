"""Detect and run the locally installed Docling CLI (``docling convert``).

Everything is executed with an argument list and ``shell=False``. The CLI of
Docling 2.131 was inspected for the options used here:

* ``--to md --image-export-mode referenced``: Markdown + external PNG files
* ``--pipeline standard --pdf-backend docling_parse``: layout model + native text
* ``--tables --table-mode accurate``: TableFormer in accurate mode
* ``--no-ocr`` / ``--ocr --ocr-engine auto``: OCR off by default
* ``--artifacts-path``: use the locally downloaded models
* ``-v`` / ``-vv``: info / debug logging

Note: the CLI exits with code 0 even when a document fails; it only logs
"Processed N docs, of which M failed". Success is therefore judged from that
line and from the produced Markdown file, never from the exit code alone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .config import DOCLING_ENV_OVERRIDES

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DOCLING_NOT_FOUND_MESSAGE = (
    "Docling was not found.\n\nPlease install Docling or add it to your PATH."
)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclass
class DoclingInfo:
    available: bool
    path: Optional[str] = None
    version: Optional[str] = None
    version_details: str = ""
    error: str = ""

    def diagnostics(self) -> str:
        lines = [
            f"Docling available: {'yes' if self.available else 'NO'}",
            f"Executable: {self.path or '(not found)'}",
            f"Version: {self.version or '(unknown)'}",
        ]
        if self.version_details:
            lines += ["", "Version probe output:", self.version_details.strip()]
        if self.error:
            lines += ["", "Error:", self.error.strip()]
        lines += ["", "PATH:", *os.environ.get("PATH", "").split(os.pathsep)]
        return "\n".join(lines)


def find_docling(explicit: str = "") -> Optional[str]:
    if explicit:
        p = Path(os.path.expandvars(explicit)).expanduser()
        if p.is_file():
            return str(p)
    return shutil.which("docling")


def child_env() -> dict:
    """Environment for Docling: ours minus PyInstaller internals, plus overrides."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("_MEI", "_PYI")) and k not in ("TCL_LIBRARY", "TK_LIBRARY")
    }
    env.update(DOCLING_ENV_OVERRIDES)
    return env


def _prepare_frozen_child() -> None:
    """A PyInstaller one-file bootloader sets a DLL directory that child
    processes can inherit; reset it so Docling loads its own DLLs."""
    if getattr(sys, "frozen", False) and os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass


def detect_docling(explicit: str = "", timeout: float = 180.0) -> DoclingInfo:
    path = find_docling(explicit)
    if not path:
        return DoclingInfo(available=False, error="'docling' is not on PATH.")
    _prepare_frozen_child()
    try:
        proc = subprocess.run(
            [path, "convert", "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=child_env(),
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return DoclingInfo(False, path, error=f"'docling convert --version' timed out after {timeout:.0f}s.")
    except OSError as exc:
        return DoclingInfo(False, path, error=f"Could not start Docling: {exc}")
    out = proc.stdout or ""
    m = re.search(r"Docling version:\s*([^\s]+)", out)
    if not m:
        return DoclingInfo(
            False, path, version_details=out,
            error=f"Unexpected output from 'docling convert --version' (exit code {proc.returncode}).",
        )
    return DoclingInfo(True, path, m.group(1), out)


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
@dataclass
class DoclingOptions:
    ocr: bool = False
    table_mode: str = "accurate"
    artifacts_path: Optional[Path] = None
    debug: bool = False
    page_range: Optional[str] = None  # e.g. "1-4"; for testing
    num_threads: Optional[int] = None


def build_docling_args(docling_exe: str, source: str, output_dir: str, opts: DoclingOptions) -> List[str]:
    """Argument list for ``docling convert``. ``source``/``output_dir`` are
    passed through verbatim (they are relative to the working directory)."""
    args = [
        docling_exe, "convert", source,
        "--from", "pdf",
        "--to", "md",
        "--image-export-mode", "referenced",
        "--pipeline", "standard",
        "--pdf-backend", "docling_parse",
        "--tables",
        "--table-mode", opts.table_mode,
    ]
    if opts.ocr:
        args += ["--ocr", "--ocr-engine", "auto"]
    else:
        args += ["--no-ocr"]
    if opts.artifacts_path:
        args += ["--artifacts-path", str(opts.artifacts_path)]
    if opts.page_range:
        args += ["--page-range", opts.page_range]
    if opts.num_threads:
        args += ["--num-threads", str(opts.num_threads)]
    args += ["--output", output_dir, "-vv" if opts.debug else "-v"]
    return args


# --------------------------------------------------------------------------- #
# Log interpretation
# --------------------------------------------------------------------------- #
@dataclass
class LogEvent:
    text: str
    level: str = "info"  # info | warning | error | debug
    stage: Optional[str] = None  # human-readable sub-stage, if the line marks one
    show: bool = True  # whether to show outside debug mode


_NOISE_RE = re.compile(
    r"(HTTP Request:|Loading weights|\d+%\|.*\||it/s\]|B/s\]|^\s*$|_client\.py|"
    r"Accelerator device|Using engine_name|device_config\.py|Registered picture descriptions|"
    r"Initializing Transformers|Transformers\s*engine ready|^\s*\[INFO\].*RapidOCR)",
    re.IGNORECASE,
)
_STAGE_PATTERNS = [
    (re.compile(r"Auto OCR model selected|RapidOCR|OCR engine", re.I), "Loading OCR models"),
    (re.compile(r"Initializing Transformers|Loading weights|layout", re.I), "Loading layout & table models"),
    (re.compile(r"Processing document", re.I), "Analyzing pages (text, layout, tables, figures)"),
    (re.compile(r"Finished converting document", re.I), "Docling finished analyzing the document"),
    (re.compile(r"writing Markdown output", re.I), "Writing Markdown and images"),
]
_PROCESSED_RE = re.compile(r"Processed\s+(\d+)\s+docs?,\s+of which\s+(\d+)\s+failed", re.I)
_TIMESTAMP_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d+\s+")


def interpret_line(raw: str) -> LogEvent:
    text = raw.rstrip()
    stage = None
    for pattern, name in _STAGE_PATTERNS:
        if pattern.search(text):
            stage = name
            break
    upper = text.upper()
    if "TRACEBACK" in upper or re.search(r"\bERROR\b|\bCRITICAL\b|Exception:|Error:", text):
        level = "error"
    elif re.search(r"\bWARNING\b|\bWARN\b", text):
        level = "warning"
    elif "DEBUG" in upper[:40]:
        level = "debug"
    else:
        level = "info"
    noisy = bool(_NOISE_RE.search(text)) and level == "info"
    show = level in ("warning", "error") or (not noisy and stage is not None) or bool(_PROCESSED_RE.search(text))
    return LogEvent(text=text, level=level, stage=stage, show=show)


def pretty_log_text(text: str) -> str:
    """Shorten a Docling log line for display (drop timestamp and logger name)."""
    t = _TIMESTAMP_RE.sub("", text)
    t = re.sub(r"^(INFO|WARNING|ERROR|DEBUG)\s+docling[\w.]*:\s*", lambda m: "" if m.group(1) == "INFO" else f"{m.group(1)}: ", t)
    return t.strip()


def parse_failed_count(lines: List[str]) -> Optional[int]:
    for line in reversed(lines):
        m = _PROCESSED_RE.search(line)
        if m:
            return int(m.group(2))
    return None


_FAILURE_HINTS = [
    (re.compile(r"password|encrypt", re.I),
     "The PDF appears to be encrypted or password-protected. Remove the password "
     "(e.g. print to PDF) and try again."),
    (re.compile(r"No space left|errno 28|disk full|not enough space|ENOSPC", re.I),
     "The disk is full. Free some space on the output drive and try again."),
    (re.compile(r"PermissionError|Permission denied|Access is denied|WinError 5\b", re.I),
     "Permission denied while Docling was reading or writing files. Check that the "
     "output folder is writable and the PDF is not locked by another program."),
    (re.compile(r"MemoryError|out of memory|Unable to allocate|CUDA out of memory", re.I),
     "Docling ran out of memory. Close other programs or convert a smaller page range."),
    (re.compile(r"not valid|invalid pdf|PdfiumError|Failed to load document|data format error|"
                r"cannot open|could not load|is not a pdf|Input document .* is not", re.I),
     "The PDF could not be opened. It may be corrupted or not a real PDF."),
    (re.compile(r"LocalEntryNotFoundError|snapshot_download|huggingface|Connection|Timeout|"
                r"getaddrinfo|SSLError", re.I),
     "Docling could not load its AI models (download or network problem). Make sure the "
     "Docling models are downloaded (docling-tools models download) and try again."),
    (re.compile(r"ModuleNotFoundError|ImportError|DLL load failed", re.I),
     "The Docling installation appears to be broken (a Python module or DLL failed to load). "
     "Reinstall or repair Docling."),
]


def explain_failure(lines: List[str], returncode: Optional[int]) -> str:
    """Human-readable explanation of a failed Docling run, from its log."""
    blob = "\n".join(lines[-400:])
    for pattern, message in _FAILURE_HINTS:
        if pattern.search(blob):
            return message
    failed = parse_failed_count(lines)
    if failed:
        return ("Docling could not convert this document. See the log for details "
                "(the document may be damaged or use unsupported features).")
    if returncode not in (0, None):
        return f"Docling stopped unexpectedly (exit code {returncode}). See the log for details."
    return "Docling finished without producing Markdown output. See the log for details."


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    returncode: Optional[int]
    cancelled: bool
    lines: List[str] = field(default_factory=list)
    failed_docs: Optional[int] = None
    start_error: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.cancelled
            and not self.start_error
            and self.returncode == 0
            and self.failed_docs in (0, None)
        )


class DoclingRunner:
    """Runs one ``docling convert`` process; ``run`` blocks (call it from a worker thread)."""

    def __init__(self, on_event: Optional[Callable[[LogEvent], None]] = None):
        self._on_event = on_event or (lambda e: None)
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    def run(self, args: List[str], cwd: Path) -> RunResult:
        lines: List[str] = []
        _prepare_frozen_child()
        with self._lock:
            if self._cancelled.is_set():
                return RunResult(None, True, lines)
            try:
                self._proc = subprocess.Popen(
                    args,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    shell=False,
                    env=child_env(),
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                return RunResult(None, False, lines, start_error=f"Could not start Docling: {exc}")
        proc = self._proc
        assert proc.stdout is not None
        # Text mode uses universal newlines, so tqdm '\r' updates arrive as lines.
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            try:
                self._on_event(interpret_line(line))
            except Exception:
                pass
        proc.wait()
        return RunResult(
            returncode=proc.returncode,
            cancelled=self._cancelled.is_set(),
            lines=lines,
            failed_docs=parse_failed_count(lines),
        )

    def cancel(self) -> None:
        """Stop Docling and every child process it started."""
        self._cancelled.set()
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=30,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
