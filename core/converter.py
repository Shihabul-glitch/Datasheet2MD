"""One document: PDF -> Docling (one pass) -> images/ -> cleaned Markdown -> checks -> publish.

The output folder contains exactly ``<pdf name>.md`` and ``images/``. Every
job works in its own staging folder on the output drive
(``<output root>/.d2md_staging/<job>/``); the finished folder is published
with an atomic rename only after the checks passed. A cancelled or failed
conversion publishes nothing; a partial one (Docling skipped pages) is kept in
a separate ``<name>_incomplete`` folder so it can't be mistaken for a complete
result. Timings, the engine configuration and warnings go to the per-job log
in the app-data folder, not into the document folder.
"""

from __future__ import annotations

import datetime as _dt
import errno
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, TextIO

from . import markdown_cleaner, output_manager
from .config import (
    APP_NAME,
    APP_VERSION,
    IMAGES_DIRNAME,
    MIN_FREE_BYTES,
    STAGING_PDF_NAME,
    logs_dir,
    resolve_artifacts_path,
)
from .docling_runner import (
    NO_OCR_ENGINE_RE,
    OCR_ENGINE_SELECTED_RE,
    DoclingInfo,
    DoclingOptions,
    DoclingRunner,
    build_docling_args,
    explain_failure,
    interpret_line,
    pretty_log_text,
)
from .engine import EngineCancelled, EngineError, EngineResult, WorkerEngine
from .image_manager import ImageError, normalize_images
from .markdown_cleaner import CleanupOptions
from .output_manager import DestinationPlan, OutputError, Reservations
from .validator import (
    ValidationReport,
    count_headings,
    count_tables,
    little_text_warning,
    sha256_file,
    validate_output,
)

# Stage names shown in the UI (only emitted when they really start).
PREPARING = "Preparing"
STARTING_ENGINE = "Starting engine"
LOADING_MODELS = "Loading models"
CONVERTING = "Converting"
SAVING = "Saving Markdown and images"
CHECKING = "Checking output"

# Job/document status values (also used in the queue).
COMPLETED = "completed"
WARNINGS = "completed_with_warnings"
PARTIAL = "partial"
FAILED = "failed"
CANCELLED = "cancelled"

LOG_FILE_LIMIT = 20 * 1024 * 1024
LOG_FILES_KEPT = 200
INCOMPLETE_SUFFIX = "_incomplete"
_WORKER_STAGES = {"starting_engine": STARTING_ENGINE, "loading_models": LOADING_MODELS,
                  "converting": CONVERTING, "saving": SAVING}


@dataclass
class ConversionRequest:
    pdf_path: Path
    output_root: Optional[Path] = None  # None: beside the PDF
    ocr: bool = False
    device: str = "cpu"  # resolved device: cpu | cuda
    llm_cleanup: bool = True
    cleanup_options: CleanupOptions = field(default_factory=CleanupOptions)
    debug: bool = False
    artifacts_setting: str = "auto"
    page_range: Optional[str] = None  # "a-b" (CLI only)
    num_threads: Optional[int] = None
    job_id: str = ""
    plan: Optional[DestinationPlan] = None  # pre-reserved by the batch runner
    reservations: Optional[Reservations] = None


@dataclass
class UiEvent:
    kind: str  # "stage" | "log"
    text: str
    level: str = "info"  # info | warning | error | success | debug


@dataclass
class ConversionResult:
    status: str = FAILED
    error_title: str = ""
    error_message: str = ""
    plan: Optional[DestinationPlan] = None
    output_dir: Optional[Path] = None
    markdown_path: Optional[Path] = None
    validation: Optional[ValidationReport] = None
    warnings: List[str] = field(default_factory=list)
    log_path: Optional[Path] = None
    seconds: float = 0.0
    page_count: Optional[int] = None
    timings: Dict[str, float] = field(default_factory=dict)
    worker_rss_mb: Optional[int] = None
    device_used: str = ""
    ocr_engine: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status in (COMPLETED, WARNINGS)

    @property
    def cancelled(self) -> bool:
        return self.status == CANCELLED


class ConversionFailed(Exception):
    def __init__(self, message: str, title: str = "Conversion failed"):
        super().__init__(message)
        self.title = title


class _Cancelled(Exception):
    pass


class CliEngine:
    """Fallback engine: one ``docling.exe convert`` process per document (v1.0 behaviour)."""

    def __init__(self, info: DoclingInfo):
        self.info = info
        self._runner: Optional[DoclingRunner] = None

    def convert(self, request: dict, on_line, on_stage) -> EngineResult:
        on_stage("converting")
        rng = request.get("page_range")
        opts = DoclingOptions(ocr=request["ocr"],
                              artifacts_path=Path(request["artifacts_path"]) if request.get("artifacts_path") else None,
                              page_range=f"{rng[0]}-{rng[1]}" if rng else None,
                              num_threads=request.get("num_threads"),
                              device=request.get("device") if request.get("device") != "cpu" else "cpu",
                              debug=request.get("debug", False))
        args = build_docling_args(self.info.path or "docling", request["source"], ".", opts)
        on_line("Command: " + " ".join(f'"{a}"' if " " in a else a for a in args))
        self._runner = DoclingRunner(on_line)
        res = self._runner.run(args, Path(request["workdir"]))
        if res.cancelled:
            raise EngineCancelled()
        if res.start_error:
            raise EngineError(res.start_error)
        md = Path(request["workdir"]) / "source.md"
        ok = res.ok and md.is_file() and md.stat().st_size > 0
        return EngineResult(status="success" if ok else "failure",
                            errors=[] if ok else [explain_failure(res.lines, res.returncode)],
                            markdown_written=md.is_file(), timings={"convert_s": round(res.seconds, 1)},
                            worker_started=True, log_lines=res.lines[-2000:])

    def cancel(self) -> None:
        if self._runner:
            self._runner.cancel()

    def close(self) -> None:
        pass


class Converter:
    def __init__(self, request: ConversionRequest, engine, info: DoclingInfo,
                 emit: Optional[Callable[[UiEvent], None]] = None):
        self.req = request
        self.engine = engine
        self.info = info
        self.job_id = request.job_id or uuid.uuid4().hex[:12]
        self._emit_cb = emit or (lambda e: None)
        self._cancel = threading.Event()
        self._log_fh: Optional[TextIO] = None
        self._log_bytes = 0
        self._stage = ""

    # ------------------------------------------------------------------ utils
    def _emit(self, kind: str, text: str, level: str = "info") -> None:
        self._write_log(f"[{kind}:{level}] {text}")
        try:
            self._emit_cb(UiEvent(kind, text, level))
        except Exception:
            pass

    def _set_stage(self, name: str) -> None:
        if name and name != self._stage:
            self._stage = name
            self._emit("stage", name)

    def _log(self, text: str, level: str = "info") -> None:
        self._emit("log", text, level)

    def _write_log(self, text: str) -> None:
        if not self._log_fh or self._log_bytes > LOG_FILE_LIMIT:
            return
        try:
            self._log_fh.write(text + "\n")
            self._log_fh.flush()
            self._log_bytes += len(text) + 1
            if self._log_bytes > LOG_FILE_LIMIT:
                self._log_fh.write("[log truncated: size limit reached]\n")
        except OSError:
            pass

    def _open_log(self, name: str) -> Optional[Path]:
        try:
            d = logs_dir()
            d.mkdir(parents=True, exist_ok=True)
            for stale in sorted(d.glob("*.log"))[:-LOG_FILES_KEPT]:
                stale.unlink(missing_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:60]
            path = d / f"{stamp}_{self.job_id[:8]}_{safe}.log"
            self._log_fh = path.open("w", encoding="utf-8")
            return path
        except OSError:
            return None

    def cancel(self) -> None:
        self._cancel.set()
        try:
            self.engine.cancel()
        except Exception:
            pass

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    def _on_engine_line(self, line: str) -> None:
        self._write_log(line)
        ev = interpret_line(line)
        if ev.show or self.req.debug:
            text = pretty_log_text(ev.text)
            if text:
                self._emit_cb(UiEvent("log", text, ev.level))

    def _on_engine_stage(self, stage: str) -> None:
        self._set_stage(_WORKER_STAGES.get(stage, stage))

    # ------------------------------------------------------------------- run
    def run(self) -> ConversionResult:
        started = time.monotonic()
        result = ConversionResult()
        t: Dict[str, float] = result.timings
        pdf = Path(self.req.pdf_path).expanduser()
        result.log_path = self._open_log(pdf.stem)
        self._write_log(f"{APP_NAME} {APP_VERSION} job {self.job_id}: {pdf}")
        stage_dir: Optional[Path] = None
        try:
            self._set_stage(PREPARING)
            mark = time.monotonic()
            pdf, size = self._check_pdf(pdf)
            hash_before = sha256_file(pdf)
            plan = self.req.plan or output_manager.plan_destination(
                pdf, self.req.output_root, self.req.reservations, self.job_id)
            result.plan = plan
            for note in plan.notes:
                self._log(note)
            output_manager.check_writable(plan.root)
            need = max(MIN_FREE_BYTES, 4 * size + 50 * 1024 * 1024)
            free = output_manager.free_bytes(plan.root)
            if free is not None and free < need:
                raise ConversionFailed(f"Not enough free disk space on the output drive ({free / 1_048_576:.0f} MB "
                                       f"free, about {need / 1_048_576:.0f} MB needed).", "Disk full")
            stage_dir = output_manager.create_staging(plan.root, self.job_id)
            shutil.copyfile(pdf, stage_dir / STAGING_PDF_NAME)
            t["prepare_s"] = round(time.monotonic() - mark, 2)
            self._check_cancel()

            run = self._run_engine(stage_dir, result)
            self._check_cancel()

            self._set_stage(SAVING)
            mark = time.monotonic()
            md = self._read_markdown(stage_dir / "source.md", result)
            tables_before, headings_before = count_tables(md), count_headings(md)
            out_dir = stage_dir / "out"
            out_dir.mkdir()
            md = self._extract_images(md, stage_dir, out_dir, result)
            md = self._clean(md, result)
            self._write_markdown(out_dir / plan.markdown_name, md)
            t["postprocess_s"] = round(time.monotonic() - mark, 2)
            self._check_cancel()

            self._set_stage(CHECKING)
            mark = time.monotonic()
            report = validate_output(out_dir / plan.markdown_name, out_dir / IMAGES_DIRNAME, pdf, hash_before,
                                     expected_tables=tables_before, expected_headings=headings_before)
            result.validation = report
            for c in report.checks:
                self._write_log(c.line())
                if c.status == "warn":
                    result.warnings.append(c.message)
            pages = run.pages or ([] if run.page_count is None else list(range(1, run.page_count + 1)))
            n_pages = len(pages) or (run.page_count or 0)
            hint = little_text_warning(report.text_chars, n_pages, self.req.ocr)
            if hint:
                self._warn(result, hint)
            self._table_warnings(run, result)
            partial = self._partial_reasons(run)
            if not report.passed:
                partial += [f"Output check failed: {c.message}" for c in report.checks if c.status == "fail"]
            t["validate_s"] = round(time.monotonic() - mark, 2)
            self._check_cancel()

            mark = time.monotonic()
            if partial:
                result.status = PARTIAL
                final = self._publish(out_dir, plan, suffix=INCOMPLETE_SUFFIX)
                result.error_title = "Incomplete conversion"
                result.error_message = "; ".join(partial[:3]) + f"\nThe incomplete output was kept in {final}"
                for r in partial:
                    self._log(r, "error")
            else:
                result.status = WARNINGS if result.warnings else COMPLETED
                final = self._publish(out_dir, plan)
            t["publish_s"] = round(time.monotonic() - mark, 2)
            result.output_dir = final
            result.markdown_path = final / plan.markdown_name
            self._log(f"Saved: {result.markdown_path} ({report.image_count} image(s), {report.table_count} table(s))",
                      "success" if result.status == COMPLETED else "warning")
        except _Cancelled:
            result.status = CANCELLED
            result.error_title = "Conversion cancelled"
            result.error_message = "The conversion was cancelled. No output was kept."
            self._log("Cancelled. No output was kept.", "warning")
        except ConversionFailed as exc:
            self._fail(result, exc.title, str(exc))
        except OutputError as exc:
            self._fail(result, "Output folder problem", str(exc))
        except EngineError as exc:
            self._fail(result, "Docling engine problem", str(exc))
        except OSError as exc:
            self._fail(result, "Conversion failed", _explain_os_error(exc))
        except Exception as exc:  # last resort: never crash the batch
            import traceback

            self._write_log(traceback.format_exc())
            self._fail(result, "Conversion failed", f"Unexpected error: {exc.__class__.__name__}: {exc}")
        finally:
            result.seconds = time.monotonic() - started
            t["total_s"] = round(result.seconds, 1)
            if stage_dir is not None:
                if self.req.debug and result.status != CANCELLED:
                    self._write_log(f"Debug mode: intermediate files kept in {stage_dir}")
                else:
                    output_manager.remove_staging(stage_dir)
            if self.req.reservations and result.status in (FAILED, CANCELLED):
                self.req.reservations.release_job(self.job_id)
            self._write_log("SUMMARY " + json.dumps({
                "status": result.status, "ocr": self.req.ocr, "device": result.device_used,
                "engine": self.info.backend, "docling": self.info.version, "torch": self.info.torch_version,
                "ocr_engine": result.ocr_engine, "pages": result.page_count, "timings": t,
                "worker_rss_mb": result.worker_rss_mb, "warnings": result.warnings}))
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None
        return result

    def _fail(self, result: ConversionResult, title: str, message: str) -> None:
        result.status = FAILED
        result.error_title = title
        result.error_message = message
        self._log(f"{title}: {message}", "error")

    def _warn(self, result: ConversionResult, text: str) -> None:
        result.warnings.append(text)
        self._log(text, "warning")

    # ----------------------------------------------------------------- steps
    def _check_pdf(self, pdf: Path):
        if not pdf.exists():
            raise ConversionFailed(f"The file '{pdf}' does not exist (moved or deleted?).", "Source missing")
        if not pdf.is_file():
            raise ConversionFailed(f"'{pdf}' is not a file.", "Invalid input")
        pdf = pdf.resolve()
        try:
            with pdf.open("rb") as fh:
                head = fh.read(1024)
            size = pdf.stat().st_size
        except PermissionError:
            raise ConversionFailed(f"Permission denied: cannot read '{pdf.name}'. Close programs that lock it.",
                                   "Permission denied")
        except OSError as exc:
            raise ConversionFailed(f"Cannot read '{pdf.name}': {exc}", "Source unreadable")
        if size == 0:
            raise ConversionFailed(f"'{pdf.name}' is empty (0 bytes).", "Invalid PDF")
        if b"%PDF-" not in head:
            raise ConversionFailed(f"'{pdf.name}' is not a valid PDF file (no PDF header found).", "Invalid PDF")
        self._log(f"Source: {pdf} ({size / 1_048_576:.2f} MB)")
        return pdf, size

    def _engine_request(self, device: str) -> dict:
        artifacts = resolve_artifacts_path(self.req.artifacts_setting)
        rng = None
        if self.req.page_range:
            a, _, b = str(self.req.page_range).partition("-")
            rng = [int(a), int(b or a)]
        return {"id": self.job_id, "source": STAGING_PDF_NAME, "markdown": "source.md",
                "ocr": bool(self.req.ocr), "device": device,
                "artifacts_path": str(artifacts) if artifacts else None, "page_range": rng,
                "num_threads": self.req.num_threads or None, "debug": self.req.debug}

    def _run_engine(self, stage: Path, result: ConversionResult) -> EngineResult:
        device = self.req.device or "cpu"
        self._log(f"Engine: Docling {self.info.version} ({'persistent worker' if self.info.backend == 'worker' else 'docling.exe'}), "
                  f"OCR {'ON' if self.req.ocr else 'off'}, device {device}, tables accurate, images PNG")
        if self.req.ocr and self.info.ocr_available is False:
            self._warn(result, "OCR was requested, but no OCR engine is installed in Docling's environment "
                               "(see Diagnostics); scanned text will be missing.")
        run = self._engine_once(stage, device, result)
        if device != "cpu" and (run.status == "device_unavailable" or run.cuda_oom):
            why = ("the GPU ran out of memory" if run.cuda_oom else "the GPU is not usable with Docling's PyTorch")
            detail = "; ".join(e.rstrip(". ") for e in run.errors)[:200]
            self._warn(result, f"Converted on the CPU because {why} ({detail}).")
            _clear_engine_outputs(stage)
            device = "cpu"
            run = self._engine_once(stage, device, result)
        result.device_used = device
        result.page_count = run.page_count or (len(run.pages) or None)
        result.worker_rss_mb = run.rss_mb
        for line in run.log_lines:
            m = OCR_ENGINE_SELECTED_RE.search(line)
            if m:
                result.ocr_engine = m.group(1).strip()
        if self.req.ocr:
            if result.ocr_engine:
                self._log(f"OCR engine: {result.ocr_engine} (Docling auto selection, region-aware OCR)")
            if any(NO_OCR_ENGINE_RE.search(l) for l in run.log_lines):
                self._warn(result, "OCR was requested but Docling found no usable OCR engine; scanned text is "
                                   "missing (see Diagnostics).")
        md = stage / "source.md"
        if run.status not in ("success", "partial_success") or not md.is_file() or md.stat().st_size == 0:
            raise ConversionFailed(explain_failure(run.errors + run.log_lines[-400:]) +
                                   (f"\nDocling reported: {'; '.join(run.errors)[:500]}" if run.errors else ""),
                                   "Docling could not convert this PDF")
        return run

    def _engine_once(self, stage: Path, device: str, result: ConversionResult) -> EngineResult:
        t = result.timings
        mark = time.monotonic()
        try:
            req = self._engine_request(device)
            req["workdir"] = str(stage)
            run = self.engine.convert(req, self._on_engine_line, self._on_engine_stage)
        except EngineCancelled:
            raise _Cancelled()
        if self._cancel.is_set():
            raise _Cancelled()
        t["engine_total_s"] = round(t.get("engine_total_s", 0) + time.monotonic() - mark, 1)
        if run.import_s is not None:
            t["engine_start_s"] = run.import_s
        if run.models_loaded_s is not None:
            t["models_load_s"] = run.models_loaded_s
        for k, v in run.timings.items():
            t[k] = v
        self._log(f"Docling: {run.timings.get('convert_s', '?')} s converting"
                  + (f", {run.timings['export_s']} s writing Markdown" if run.timings.get("export_s") else "")
                  + (f", models loaded in {run.models_loaded_s} s" if run.models_loaded_s else "")
                  + (f", engine started in {run.import_s} s" if run.import_s else "")
                  + (f"; worker memory {run.rss_mb} MB" if run.rss_mb else ""))
        return run

    def _partial_reasons(self, run: EngineResult) -> List[str]:
        reasons = []
        if run.status == "partial_success":
            reasons.append("Docling reported a PARTIAL conversion"
                           + (": " + "; ".join(e for e in run.errors[:3] if e) if run.errors else ""))
        if run.page_count and run.pages:
            if self.req.page_range:
                a, _, b = str(self.req.page_range).partition("-")
                wanted = range(int(a), min(int(b or a), run.page_count) + 1)
            else:
                wanted = range(1, run.page_count + 1)
            missing = [p for p in wanted if p not in set(run.pages)]
            if missing:
                reasons.append(f"{len(missing)} page(s) missing from the output: "
                               + ", ".join(str(p) for p in missing[:20]) + (" ..." if len(missing) > 20 else ""))
        return reasons

    def _table_warnings(self, run: EngineResult, result: ConversionResult) -> None:
        merged = [t for t in run.tables if t.get("merged")]
        if merged:
            pages = sorted({t.get("page") for t in merged if t.get("page")})
            self._warn(result, f"{len(merged)} table(s) contain merged cells (page(s) {', '.join(map(str, pages))}). "
                               "Markdown cannot show merged cells, so the merged value is repeated in each "
                               "spanned column; check these tables against the PDF.")

    def _read_markdown(self, path: Path, result: ConversionResult) -> str:
        raw = path.read_bytes()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self._warn(result, "Docling output was not valid UTF-8; invalid bytes were replaced (�).")
            return raw.decode("utf-8", errors="replace")

    def _extract_images(self, md: str, stage: Path, out_dir: Path, result: ConversionResult) -> str:
        md, n_b64, warnings = markdown_cleaner.extract_embedded_images(md, stage / "embedded", "embedded")
        for w in warnings:
            self._warn(result, w)
        try:
            img = normalize_images(md, stage, out_dir / IMAGES_DIRNAME, IMAGES_DIRNAME)
        except ImageError as exc:
            raise ConversionFailed(f"Image export failed: {exc}")
        if img.missing:
            self._warn(result, f"{len(img.missing)} image file(s) referenced by Docling were missing; "
                               "replaced by a visible note.")
        self._log(f"{img.count} image(s) saved to {IMAGES_DIRNAME}/")
        return img.markdown

    def _clean(self, md: str, result: ConversionResult) -> str:
        try:
            cleaned, report = markdown_cleaner.clean_markdown(md, self.req.llm_cleanup, self.req.cleanup_options)
        except Exception as exc:
            self._warn(result, f"Markdown cleanup failed ({exc}); the uncleaned Markdown was kept.")
            return markdown_cleaner.basic_normalize(md)
        if self.req.llm_cleanup:
            for item in report.summary():
                self._write_log(f"Cleanup: {item}")
        if count_tables(cleaned) < count_tables(md):
            self._warn(result, "Cleanup would have altered tables; the uncleaned Markdown was kept.")
            return markdown_cleaner.basic_normalize(md)
        return cleaned

    def _write_markdown(self, path: Path, md: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(md)
        os.replace(tmp, path)

    def _publish(self, out_dir: Path, plan: DestinationPlan, suffix: str = "") -> Path:
        reservations = self.req.reservations or Reservations()
        if suffix:
            plan = DestinationPlan(plan.source_pdf, plan.root, plan.base_dir.with_name(plan.base_dir.name + suffix),
                                   plan.target_dir.with_name(plan.base_dir.name + suffix), plan.markdown_name)
        return output_manager.publish_new(out_dir, plan, reservations, self.job_id)


def _clear_engine_outputs(stage: Path) -> None:
    for name in ("source.md",):
        try:
            (stage / name).unlink()
        except OSError:
            pass
    shutil.rmtree(stage / "source_artifacts", ignore_errors=True)


def _explain_os_error(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112):
        return "The disk is full. Free some space on the output drive and try again."
    if getattr(exc, "winerror", None) == 206 or exc.errno == errno.ENAMETOOLONG:
        return (f"A path is too long for Windows: {exc.filename or exc}. Choose a shorter output folder "
                "or enable Windows long paths.")
    if isinstance(exc, PermissionError):
        return (f"Permission denied: {exc.filename or exc}. Choose a writable output folder "
                "and close programs that may lock these files.")
    if isinstance(exc, FileNotFoundError):
        return f"File or folder not found: {exc.filename or exc}."
    return f"File system error: {exc}"
