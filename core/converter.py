"""End-to-end conversion: PDF -> Docling -> images/ -> cleaned Markdown -> validation.

The GUI and the command-line mode both drive this class. ``Converter.run`` is
blocking; the GUI calls it from a worker thread and receives progress through
the ``emit`` callback.
"""

from __future__ import annotations

import datetime as _dt
import errno
import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, TextIO

from . import markdown_cleaner, output_manager
from .config import (
    DEBUG_REPORT_NAME,
    MIN_FREE_BYTES,
    STAGING_PDF_NAME,
    logs_dir,
    resolve_artifacts_path,
)
from .docling_runner import (
    DoclingOptions,
    DoclingRunner,
    LogEvent,
    build_docling_args,
    explain_failure,
    pretty_log_text,
)
from .image_manager import ImageError, normalize_images
from .output_manager import ExistsPolicy, OutputError, OutputPlan
from .validator import ValidationReport, count_headings, count_tables, sha256_file, validate_output

STAGES = [
    "Preparing",
    "Running Docling",
    "Processing output",
    "Extracting images",
    "Cleaning Markdown",
    "Finalizing",
    "Completed",
]


@dataclass
class ConversionRequest:
    pdf_path: Path
    output_root: Optional[Path] = None  # None: same directory as the PDF
    policy: ExistsPolicy = ExistsPolicy.NEW_VERSION
    ocr: bool = False
    llm_cleanup: bool = True
    debug: bool = False
    artifacts_setting: str = "auto"
    page_range: Optional[str] = None


@dataclass
class UiEvent:
    kind: str  # "stage" | "substage" | "log"
    text: str
    level: str = "info"  # info | warning | error | success | debug


@dataclass
class ConversionResult:
    success: bool = False
    cancelled: bool = False
    error_title: str = ""
    error_message: str = ""
    plan: Optional[OutputPlan] = None
    validation: Optional[ValidationReport] = None
    warnings: List[str] = field(default_factory=list)
    log_path: Optional[Path] = None
    seconds: float = 0.0

    @property
    def output_dir(self) -> Optional[Path]:
        return self.plan.component_dir if self.plan else None


class ConversionFailed(Exception):
    def __init__(self, message: str, title: str = "Conversion failed", keep_output: bool = False):
        super().__init__(message)
        self.title = title
        self.keep_output = keep_output


class Converter:
    def __init__(self, request: ConversionRequest, docling_exe: str,
                 emit: Optional[Callable[[UiEvent], None]] = None):
        self.req = request
        self.docling_exe = docling_exe
        self._emit_cb = emit or (lambda e: None)
        self._runner: Optional[DoclingRunner] = None
        self._cancel = threading.Event()
        self._log_fh: Optional[TextIO] = None

    # ------------------------------------------------------------------ utils
    def _emit(self, kind: str, text: str, level: str = "info") -> None:
        self._write_log(f"[{kind}:{level}] {text}")
        try:
            self._emit_cb(UiEvent(kind, text, level))
        except Exception:
            pass

    def _stage(self, name: str) -> None:
        self._emit("stage", name)

    def _log(self, text: str, level: str = "info") -> None:
        self._emit("log", text, level)

    def _write_log(self, text: str) -> None:
        if self._log_fh:
            try:
                self._log_fh.write(text + "\n")
                self._log_fh.flush()
            except OSError:
                pass

    def _open_log(self, name: str) -> Optional[Path]:
        try:
            d = logs_dir()
            d.mkdir(parents=True, exist_ok=True)
            old = sorted(d.glob("*.log"))
            for stale in old[:-30]:
                stale.unlink(missing_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:60]
            path = d / f"{stamp}_{safe}.log"
            self._log_fh = path.open("w", encoding="utf-8")
            return path
        except OSError:
            return None

    def cancel(self) -> None:
        self._cancel.set()
        if self._runner:
            self._runner.cancel()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled()

    def _on_docling_event(self, ev: LogEvent) -> None:
        self._write_log(ev.text)
        if ev.stage:
            self._emit_cb(UiEvent("substage", ev.stage))
        if ev.show or self.req.debug:
            text = pretty_log_text(ev.text)
            if text:
                self._emit_cb(UiEvent("log", text, ev.level if ev.level != "debug" else "debug"))

    # ------------------------------------------------------------------- run
    def run(self) -> ConversionResult:
        started = _dt.datetime.now()
        result = ConversionResult()
        pdf = Path(self.req.pdf_path).expanduser()
        result.log_path = self._open_log(pdf.stem)
        plan: Optional[OutputPlan] = None
        try:
            self._stage("Preparing")
            pdf = self._check_pdf(pdf)
            hash_before = sha256_file(pdf)
            plan = self._prepare(pdf)
            result.plan = plan
            self._check_cancel()

            self._stage("Running Docling")
            staged_md = self._run_docling(plan)
            self._check_cancel()

            self._stage("Processing output")
            md = self._read_markdown(staged_md)
            tables_before = count_tables(md)
            headings_before = count_headings(md)
            self._log(f"Docling produced {len(md):,} characters, {tables_before} tables, {headings_before} headings.")

            self._stage("Extracting images")
            md = self._extract_images(md, plan, result)
            self._check_cancel()

            self._stage("Cleaning Markdown")
            md = self._clean(md, result)

            self._stage("Finalizing")
            self._write_markdown(plan.markdown_path, md)
            report = validate_output(
                plan.markdown_path, plan.images_dir, pdf, hash_before,
                expected_tables=tables_before, expected_headings=headings_before,
            )
            result.validation = report
            if self.req.debug:
                self._write_debug_report(plan, result, report)
            else:
                shutil.rmtree(plan.staging_dir, ignore_errors=True)
            if not report.passed:
                failed = [c.message for c in report.checks if c.status == "fail"]
                raise ConversionFailed(
                    "Validation failed: " + "; ".join(failed)
                    + f"\n\nThe output was kept for inspection in:\n{plan.component_dir}",
                    keep_output=True)
            report.add("ok", "Conversion completed")
            result.success = True
            self._stage("Completed")
            self._log("Conversion completed successfully", "success")
        except _Cancelled:
            result.cancelled = True
            result.error_title = "Conversion cancelled"
            result.error_message = "The conversion was cancelled. No output was kept."
            self._log("Conversion cancelled.", "warning")
            if plan:
                output_manager.discard_output(plan)
        except ConversionFailed as exc:
            result.error_title = exc.title
            result.error_message = str(exc)
            self._log(f"Conversion failed: {exc}", "error")
            if not exc.keep_output:
                self._cleanup_failure(plan)
        except OutputError as exc:
            result.error_title = "Conversion failed"
            result.error_message = str(exc)
            self._log(f"Conversion failed: {exc}", "error")
            self._cleanup_failure(plan)
        except OSError as exc:
            result.error_title = "Conversion failed"
            result.error_message = _explain_os_error(exc)
            self._log(f"Conversion failed: {result.error_message}", "error")
            self._cleanup_failure(plan)
        except Exception as exc:  # last resort: never crash the GUI thread
            import traceback

            self._write_log(traceback.format_exc())
            result.error_title = "Conversion failed"
            result.error_message = f"Unexpected error: {exc.__class__.__name__}: {exc}"
            self._log(f"Conversion failed: {result.error_message}", "error")
            self._cleanup_failure(plan)
        finally:
            result.seconds = (_dt.datetime.now() - started).total_seconds()
            if result.log_path:
                self._write_log(f"Finished in {result.seconds:.1f}s, success={result.success}")
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None
        return result

    # ----------------------------------------------------------------- steps
    def _check_pdf(self, pdf: Path) -> Path:
        if not pdf.exists():
            raise ConversionFailed(f"The file '{pdf}' does not exist.")
        if not pdf.is_file():
            raise ConversionFailed(f"'{pdf}' is not a file.")
        pdf = pdf.resolve()
        try:
            with pdf.open("rb") as fh:
                head = fh.read(1024)
                size = pdf.stat().st_size
        except PermissionError:
            raise ConversionFailed(f"Permission denied: cannot read '{pdf.name}'. Close programs that lock it.")
        except OSError as exc:
            raise ConversionFailed(f"Cannot read '{pdf.name}': {exc}")
        if size == 0:
            raise ConversionFailed(f"'{pdf.name}' is empty (0 bytes).", "Invalid PDF")
        if b"%PDF-" not in head:
            raise ConversionFailed(f"'{pdf.name}' is not a valid PDF file (no PDF header found).", "Invalid PDF")
        if _looks_encrypted(pdf):
            self._log("The PDF has an encryption dictionary (often just copy/print restrictions). "
                      "If conversion fails, the file may be password-protected.", "warning")
        self._log(f"Source: {pdf} ({size / 1_048_576:.2f} MB)")
        return pdf

    def _prepare(self, pdf: Path) -> OutputPlan:
        plan = output_manager.plan_output(pdf, self.req.output_root, self.req.policy)
        root = plan.component_dir.parent
        output_manager.check_writable(root)
        free = output_manager.free_bytes(root)
        if free is not None and free < MIN_FREE_BYTES:
            raise ConversionFailed(
                f"Not enough free disk space on the output drive ({free / 1_048_576:.0f} MB free).",
                "Disk full")
        output_manager.prepare_output(plan)
        self._log(f"Output folder: {plan.component_dir}")
        shutil.copyfile(pdf, plan.staging_dir / STAGING_PDF_NAME)
        return plan

    def _run_docling(self, plan: OutputPlan) -> Path:
        artifacts = resolve_artifacts_path(self.req.artifacts_setting)
        opts = DoclingOptions(
            ocr=self.req.ocr,
            artifacts_path=artifacts,
            debug=self.req.debug,
            page_range=self.req.page_range,
        )
        args = build_docling_args(self.docling_exe, STAGING_PDF_NAME, ".", opts)
        self._write_log("Command: " + " ".join(f'"{a}"' if " " in a else a for a in args))
        self._log(f"OCR: {'enabled (scanned PDF mode)' if self.req.ocr else 'off (native PDF text)'}; "
                  f"tables: accurate; images: referenced PNG")
        if artifacts:
            self._log(f"Using local Docling models: {artifacts}")
        self._log("Starting Docling (this can take several minutes for long datasheets)...")
        self._runner = DoclingRunner(self._on_docling_event)
        if self._cancel.is_set():
            raise _Cancelled()
        res = self._runner.run(args, plan.staging_dir)
        if res.cancelled or self._cancel.is_set():
            raise _Cancelled()
        if res.start_error:
            raise ConversionFailed(res.start_error)
        staged_md = plan.staging_dir / (Path(STAGING_PDF_NAME).stem + ".md")
        if not res.ok or not staged_md.is_file() or staged_md.stat().st_size == 0:
            raise ConversionFailed(explain_failure(res.lines, res.returncode))
        self._log("Docling finished.", "success")
        return staged_md

    def _read_markdown(self, path: Path) -> str:
        raw = path.read_bytes()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self._log("Docling output was not valid UTF-8; invalid bytes were replaced.", "warning")
            return raw.decode("utf-8", errors="replace")

    def _extract_images(self, md: str, plan: OutputPlan, result: ConversionResult) -> str:
        md, n_b64, warnings = markdown_cleaner.extract_embedded_images(
            md, plan.staging_dir / "embedded", "embedded")
        for w in warnings:
            self._log(w, "warning")
            result.warnings.append(w)
        try:
            img = normalize_images(md, plan.staging_dir, plan.images_dir, "images")
        except ImageError as exc:
            raise ConversionFailed(f"Image export failed: {exc}")
        if img.missing:
            msg = f"{len(img.missing)} image file(s) referenced by Docling were missing; replaced by a note."
            self._log(msg, "warning")
            result.warnings.append(msg)
        if img.remote:
            self._log(f"{len(img.remote)} remote image link(s) left unchanged.", "warning")
        self._log(f"Exported {img.count} image(s) to images/ as image_001.png ...")
        return img.markdown

    def _clean(self, md: str, result: ConversionResult) -> str:
        try:
            cleaned, report = markdown_cleaner.clean_markdown(md, self.req.llm_cleanup)
        except Exception as exc:
            msg = f"Markdown cleanup failed ({exc}); the uncleaned Markdown was kept."
            self._log(msg, "warning")
            result.warnings.append(msg)
            return markdown_cleaner.basic_normalize(md)
        if self.req.llm_cleanup:
            for item in report.summary() or ["No cleanup changes were necessary"]:
                self._log(f"Cleanup: {item}")
        else:
            self._log("LLM-friendly cleanup disabled; only mandatory image handling was applied.")
        if count_tables(cleaned) < count_tables(md):
            msg = "Cleanup would have altered tables; the uncleaned Markdown was kept."
            self._log(msg, "warning")
            result.warnings.append(msg)
            return markdown_cleaner.basic_normalize(md)
        return cleaned

    def _write_markdown(self, path: Path, md: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(md)
        os.replace(tmp, path)
        self._log(f"Markdown written: {path.name} ({path.stat().st_size / 1024:.1f} KB)")

    def _write_debug_report(self, plan: OutputPlan, result: ConversionResult, report: ValidationReport) -> None:
        data = {
            "source_pdf": str(plan.source_pdf),
            "markdown": str(plan.markdown_path),
            "images": report.image_count,
            "tables": report.table_count,
            "headings": report.heading_count,
            "checks": [{"status": c.status, "message": c.message} for c in report.checks],
            "warnings": result.warnings,
            "log": str(result.log_path) if result.log_path else None,
            "staging_kept": str(plan.staging_dir),
        }
        try:
            (plan.component_dir / DEBUG_REPORT_NAME).write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _cleanup_failure(self, plan: Optional[OutputPlan]) -> None:
        if plan is None:
            return
        if self.req.debug:
            self._log(f"Debug mode: intermediate files kept in {plan.staging_dir}", "warning")
            return
        output_manager.discard_output(plan)


class _Cancelled(Exception):
    pass


def _looks_encrypted(pdf: Path) -> bool:
    """Cheap check for an /Encrypt entry in the trailer area of the file."""
    try:
        size = pdf.stat().st_size
        with pdf.open("rb") as fh:
            if size > 4 * 1024 * 1024:
                fh.seek(size - 1024 * 1024)
            return b"/Encrypt" in fh.read()
    except OSError:
        return False


def _explain_os_error(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112):
        return "The disk is full. Free some space on the output drive and try again."
    if isinstance(exc, PermissionError):
        return (f"Permission denied: {exc.filename or exc}. Choose a writable output folder "
                "and close programs that may lock these files.")
    if isinstance(exc, FileNotFoundError):
        return f"File or folder not found: {exc.filename or exc}."
    return f"File system error: {exc}"
