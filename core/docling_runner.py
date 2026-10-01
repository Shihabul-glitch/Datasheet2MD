"""Detect the locally installed Docling and run its CLI (fallback engine).

Detection first locates ``docling.exe`` and the Python interpreter that owns
it (pip launchers embed ``#!<python.exe>``), then runs
``docling_worker.py --probe`` in that interpreter (fast, no model loading).
If the interpreter can't be found, ``docling convert --version`` is used and
conversions fall back to the plain CLI:

    docling convert source.pdf --from pdf --to md --image-export-mode referenced
        --pipeline standard --pdf-backend docling_parse --tables --table-mode accurate
        --no-ocr | --ocr --ocr-engine auto  [--artifacts-path DIR] [--page-range a-b] --output . -v

(the Datasheet2MD 1.0 command, verified against Docling 2.131's ``--help``).
The CLI exits with code 0 even when a document fails, so success is judged
from its "of which N failed" line and the produced file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from .config import DOCLING_ENV_OVERRIDES, DOCLING_TESTED_VERSION, resource_path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DOCLING_NOT_FOUND_MESSAGE = (
    "Docling was not found.\n\nInstall Docling (pip install docling) so that 'docling convert --version' "
    "works in a new terminal, then press Retry."
)
WORKER_SCRIPT = "core/docling_worker.py"


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclass
class DoclingInfo:
    available: bool
    path: Optional[str] = None  # docling.exe
    version: Optional[str] = None
    version_details: str = ""
    error: str = ""
    python: Optional[str] = None  # Docling's interpreter (worker backend)
    backend: str = "cli"  # "worker" (persistent, preferred) | "cli" (fallback)
    probe: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ocr_available(self) -> Optional[bool]:
        if not self.probe:
            return None
        return bool(self.probe.get("ocr_auto_engine"))

    @property
    def ocr_engine(self) -> Optional[str]:
        return self.probe.get("ocr_auto_engine") if self.probe else None

    @property
    def torch_version(self) -> Optional[str]:
        return self.probe.get("torch") if self.probe else None

    def version_note(self) -> str:
        if not self.version or self.version == DOCLING_TESTED_VERSION:
            return ""
        return (f"Docling {self.version} is not the version this release was verified with "
                f"({DOCLING_TESTED_VERSION}); conversions should work, but report problems.")

    def diagnostics(self) -> str:
        lines = [
            f"Docling available: {'yes' if self.available else 'NO'}",
            f"Executable: {self.path or '(not found)'}",
            f"Version: {self.version or '(unknown)'} (verified with {DOCLING_TESTED_VERSION})",
            f"Engine: {'persistent worker in Docling’s Python (models stay loaded during a batch)' if self.backend == 'worker' else 'docling.exe per document (fallback; slower, no partial-page detection)'}",
            f"Docling Python: {self.python or '(not found)'}",
            f"Detection time: {self.seconds:.1f}s",
        ]
        if self.probe:
            e = self.probe.get("ocr_engines", {})
            lines += [
                f"docling-core {self.probe.get('docling_core')}, docling-parse {self.probe.get('docling_parse')}, "
                f"docling-ibm-models {self.probe.get('docling_ibm_models')}, torch {self.probe.get('torch')}",
                "",
                "OCR (only used when 'Scanned PDF / Enable OCR' is ticked):",
                f"  RapidOCR: {'installed ' + str(self.probe.get('rapidocr_version')) if e.get('rapidocr') else 'not installed'}"
                + (f" (onnxruntime: {'yes' if e.get('rapidocr_backend_onnxruntime') else 'no'})" if e.get("rapidocr") else ""),
                f"  EasyOCR: {'installed' if e.get('easyocr') else 'not installed'}",
                f"  Tesseract: {e.get('tesseract_cli') or ('tesserocr' if e.get('tesserocr') else 'not installed')}",
                f"  Docling will use: {self.ocr_engine or 'NONE - OCR unavailable'}",
            ]
            if not self.ocr_engine:
                lines += [f'  To enable OCR: "{self.python or "python"}" -m pip install rapidocr onnxruntime']
        if self.version_note():
            lines += ["", self.version_note()]
        if self.version_details:
            lines += ["", "Version probe output:", self.version_details.strip()[-3000:]]
        if self.error:
            lines += ["", "Note:" if self.available else "Error:", self.error.strip()]
        return "\n".join(lines)


def find_docling(explicit: str = "") -> Optional[str]:
    if explicit:
        p = Path(os.path.expandvars(explicit)).expanduser()
        if p.is_file():
            return str(p)
    return shutil.which("docling")


def find_docling_python(docling_exe: Optional[str]) -> Optional[str]:
    """Locate the Python interpreter that owns a pip-installed ``docling`` launcher."""
    if not docling_exe:
        return None
    exe = Path(docling_exe)
    try:
        data = exe.read_bytes()[-65536:] if exe.stat().st_size < 50_000_000 else b""
        matches = re.findall(rb"#!\s*\"?([^\r\n\"]+?python[^\r\n\"]*?\.exe)\"?\s*\r?\n", data, re.IGNORECASE)
        if matches:
            cand = Path(matches[-1].decode("utf-8", "replace").strip())
            if cand.is_file():
                return str(cand)
    except OSError:
        pass
    scripts = exe.parent
    for cand in (scripts / "python.exe", scripts.parent / "python.exe", scripts / "python3.exe"):
        if cand.is_file():
            return str(cand)
    return None


def child_env() -> dict:
    """Environment for Docling: ours minus PyInstaller internals, plus overrides."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("_MEI", "_PYI")) and k not in ("TCL_LIBRARY", "TK_LIBRARY", "PYTHONHOME", "PYTHONPATH")
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


def worker_script_path() -> Path:
    return resource_path(WORKER_SCRIPT)


def _run_quiet(args: List[str], timeout: float) -> subprocess.CompletedProcess:
    _prepare_frozen_child()
    return subprocess.run(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=timeout, shell=False, env=child_env(),
        creationflags=CREATE_NO_WINDOW,
    )


def probe_worker(python: str, timeout: float = 60.0) -> dict:
    """Run the worker's --probe mode; raises RuntimeError with a readable message."""
    script = worker_script_path()
    if not script.is_file():
        raise RuntimeError(f"Worker script missing: {script}")
    try:
        proc = _run_quiet([python, "-I", str(script), "--probe"], timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Probing Docling's Python timed out after {timeout:.0f}s.") from None
    except OSError as exc:
        raise RuntimeError(f"Could not start Docling's Python: {exc}") from None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("D2MD_PROBE "):
            return json.loads(line[len("D2MD_PROBE "):])
    raise RuntimeError(f"Unexpected probe output (exit code {proc.returncode}): {(proc.stdout or '')[-500:]}")


def detect_docling(explicit: str = "", timeout: float = 120.0, backend: str = "auto") -> DoclingInfo:
    """Find Docling. Fast path: probe Docling's interpreter (no model loading)."""
    started = time.monotonic()
    info = _detect(explicit, timeout, backend)
    info.seconds = time.monotonic() - started
    return info


def _detect(explicit: str, timeout: float, backend: str) -> DoclingInfo:
    path = find_docling(explicit)
    if not path:
        return DoclingInfo(available=False, error="'docling' is not on PATH and no docling_path is configured.")
    python = find_docling_python(path)
    probe_error = ""
    if python and backend in ("auto", "worker"):
        try:
            probe = probe_worker(python, min(timeout, 60.0))
            if probe.get("docling") and probe.get("api_module"):
                return DoclingInfo(True, path, probe["docling"], "", "", python, "worker", probe)
            probe_error = "Docling's Python does not contain the docling package."
        except (RuntimeError, ValueError) as exc:
            probe_error = str(exc)
    elif backend == "worker":
        probe_error = "Docling's Python interpreter could not be located."

    # Fallback: ask the CLI itself (slower: imports Docling).
    try:
        proc = _run_quiet([path, "convert", "--version"], timeout)
    except subprocess.TimeoutExpired:
        return DoclingInfo(False, path, error=f"'docling convert --version' timed out after {timeout:.0f}s."
                           + (f"\nWorker probe: {probe_error}" if probe_error else ""), python=python)
    except OSError as exc:
        return DoclingInfo(False, path, error=f"Could not start Docling: {exc}", python=python)
    out = proc.stdout or ""
    m = re.search(r"Docling version:\s*([^\s]+)", out)
    if not m:
        return DoclingInfo(False, path, version_details=out, python=python,
                           error=f"Unexpected output from 'docling convert --version' (exit code {proc.returncode})."
                           + (f"\nWorker probe: {probe_error}" if probe_error else ""))
    info = DoclingInfo(True, path, m.group(1), out, "", python, "cli")
    if probe_error:
        info.error = f"Persistent worker unavailable ({probe_error}); using docling.exe per document."
    return info


# --------------------------------------------------------------------------- #
# CLI fallback
# --------------------------------------------------------------------------- #
@dataclass
class DoclingOptions:
    ocr: bool = False
    table_mode: str = "accurate"
    artifacts_path: Optional[Path] = None
    debug: bool = False
    page_range: Optional[str] = None  # e.g. "1-4"
    num_threads: Optional[int] = None
    device: Optional[str] = None  # cpu | cuda (None = Docling default)


def build_docling_args(docling_exe: str, source: str, output_dir: str, opts: DoclingOptions) -> List[str]:
    """Argument list for ``docling convert`` (Markdown only, one pass)."""
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
    args += ["--ocr", "--ocr-engine", "auto"] if opts.ocr else ["--no-ocr"]
    if opts.artifacts_path:
        args += ["--artifacts-path", str(opts.artifacts_path)]
    if opts.page_range:
        args += ["--page-range", opts.page_range]
    if opts.num_threads:
        args += ["--num-threads", str(opts.num_threads)]
    if opts.device:
        args += ["--device", opts.device]
    args += ["--output", output_dir, "-vv" if opts.debug else "-v"]
    return args


# --------------------------------------------------------------------------- #
# Log interpretation
# --------------------------------------------------------------------------- #
@dataclass
class LogEvent:
    text: str
    level: str = "info"  # info | warning | error | debug
    show: bool = True  # whether to show outside debug mode


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NOISE_RE = re.compile(
    r"(HTTP Request:|Loading weights|\d+%\|.*\||it/s\]|B/s\]|^\s*$|_client\.py|"
    r"Accelerator device|Using engine_name|device_config\.py|Registered picture descriptions|"
    r"Initializing Transformers|Transformers\s*engine ready|^\s*\[INFO\].*RapidOCR|"
    r"cannot be used because .* is not installed|Loading plugin|detected formats|Going to convert|"
    r"Registered (?:ocr|layout|table structure) engines|Initializing pipeline for)",
    re.IGNORECASE,
)
_PROCESSED_RE = re.compile(r"Processed\s+(\d+)\s+docs?,\s+of which\s+(\d+)\s+failed", re.I)
_TIMESTAMP_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d+\s+")
NO_OCR_ENGINE_RE = re.compile(r"No OCR engine found", re.I)
OCR_ENGINE_SELECTED_RE = re.compile(r"Auto OCR model selected\s+(.+?)\.?\s*$", re.I)
EMPTY_OCR_RE = re.compile(r"RapidOCR returned empty result", re.I)


def interpret_line(raw: str) -> LogEvent:
    """Classify one Docling log line. Lines never change the displayed stage:
    stages come from the worker's explicit events, not from log text."""
    text = _ANSI_RE.sub("", raw.rstrip())
    upper = text.upper()
    if "TRACEBACK" in upper or re.search(r"\bERROR\b|\bCRITICAL\b|Exception:|Error:", text):
        level = "error"
    elif re.search(r"\bWARNING\b|\bWARN\b", text):
        level = "warning"
    elif "DEBUG" in upper[:40]:
        level = "debug"
    else:
        level = "info"
    if EMPTY_OCR_RE.search(text):
        level = "debug"  # harmless: OCR found no text in a figure region
    noisy = bool(_NOISE_RE.search(text)) and level == "info"
    return LogEvent(text=text, level=level, show=level in ("warning", "error") or not noisy)


def pretty_log_text(text: str) -> str:
    """Shorten a Docling log line for display (drop timestamp and logger name)."""
    t = _TIMESTAMP_RE.sub("", _ANSI_RE.sub("", text))
    t = re.sub(r"^(INFO|WARNING|ERROR|DEBUG)\s+docling[\w.]*:\s*",
               lambda m: "" if m.group(1) == "INFO" else f"{m.group(1)}: ", t)
    return t.strip()


def parse_failed_count(lines: List[str]) -> Optional[int]:
    for line in reversed(lines):
        m = _PROCESSED_RE.search(line)
        if m:
            return int(m.group(2))
    return None


_FAILURE_HINTS = [
    (re.compile(r"No such option|Invalid value for|Usage: docling", re.I),
     "The installed Docling version does not accept an option this app uses. Update Docling "
     f"(verified with {DOCLING_TESTED_VERSION}) or see Diagnostics."),
    (re.compile(r"password|encrypt", re.I),
     "The PDF appears to be encrypted or password-protected. Remove the password "
     "(e.g. print to PDF) and try again."),
    (re.compile(r"No space left|errno 28|disk full|not enough space|ENOSPC", re.I),
     "The disk is full. Free some space on the output drive and try again."),
    (re.compile(r"PermissionError|Permission denied|Access is denied|WinError 5\b", re.I),
     "Permission denied while reading or writing files. Check that the output folder is writable "
     "and the PDF is not locked by another program."),
    (re.compile(r"CUDA out of memory|out of memory|MemoryError|Unable to allocate|bad_alloc", re.I),
     "Docling ran out of memory. Close other programs, or convert a smaller page range."),
    (re.compile(r"not valid|invalid pdf|PdfiumError|Failed to load document|data format error|"
                r"cannot open|could not load|is not a pdf|Input document .* is not", re.I),
     "The PDF could not be opened. It may be damaged or not a real PDF."),
    (re.compile(r"LocalEntryNotFoundError|snapshot_download|huggingface|ConnectionError|getaddrinfo|SSLError|"
                r"safetensors|model.*not found|No such file.*model", re.I),
     "Docling could not load its AI models (missing model files, or a download/network problem). "
     "Run 'docling-tools models download' once, then see Diagnostics."),
    (re.compile(r"ModuleNotFoundError|ImportError|DLL load failed", re.I),
     "The Docling installation appears to be broken (a Python module or DLL failed to load). "
     "Reinstall or repair Docling."),
]


def explain_failure(lines: List[str], returncode: Optional[int] = None) -> str:
    """Human-readable explanation of a failed Docling run, from its log/errors."""
    blob = "\n".join(_ANSI_RE.sub("", l) for l in lines[-400:])
    for pattern, message in _FAILURE_HINTS:
        if pattern.search(blob):
            return message
    if parse_failed_count(lines):
        return ("Docling could not convert this document. See the log for details "
                "(the document may be damaged or use unsupported features).")
    if returncode not in (0, None):
        return f"Docling stopped unexpectedly (exit code {returncode}). See the log for details."
    return "Docling finished without producing Markdown output. See the log for details."


# --------------------------------------------------------------------------- #
# Running the CLI (fallback engine)
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    returncode: Optional[int]
    cancelled: bool
    lines: List[str] = field(default_factory=list)
    failed_docs: Optional[int] = None
    start_error: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return (not self.cancelled and not self.start_error and self.returncode == 0
                and self.failed_docs in (0, None))


MAX_KEPT_LINES = 20000


class DoclingRunner:
    """Runs one ``docling convert`` process; ``run`` blocks (call it from a worker thread)."""

    def __init__(self, on_line: Optional[Callable[[str], None]] = None):
        self._on_line = on_line or (lambda s: None)
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    def run(self, args: List[str], cwd: Path) -> RunResult:
        lines: List[str] = []
        started = time.monotonic()
        _prepare_frozen_child()
        with self._lock:
            if self._cancelled.is_set():
                return RunResult(None, True, lines)
            try:
                self._proc = subprocess.Popen(
                    args, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", bufsize=1, shell=False,
                    env=child_env(), creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                return RunResult(None, False, lines, start_error=f"Could not start Docling: {exc}")
        proc = self._proc
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if len(lines) > MAX_KEPT_LINES:
                del lines[: len(lines) - MAX_KEPT_LINES]
            try:
                self._on_line(line)
            except Exception:
                pass
        proc.wait()
        return RunResult(returncode=proc.returncode, cancelled=self._cancelled.is_set(), lines=lines,
                         failed_docs=parse_failed_count(lines), seconds=time.monotonic() - started)

    def cancel(self) -> None:
        """Stop Docling and every child process it started."""
        self._cancelled.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._kill_tree(proc)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Kill an app-owned process and its children (taskkill /T), nothing else."""
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, timeout=30,
                               creationflags=CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(10)
        except (subprocess.TimeoutExpired, OSError):
            pass
