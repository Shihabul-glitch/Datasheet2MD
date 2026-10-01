"""Application bootstrap: GUI, headless command line, self-test."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from core.config import APP_NAME, APP_VERSION, Settings


def run_gui(initial_paths: Optional[List[str]] = None) -> int:
    import customtkinter as ctk

    from ui.main_window import MainWindow

    _set_app_user_model_id()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    window = MainWindow(Settings.load(), initial_paths=initial_paths or [])
    window.mainloop()
    return 0


def _say(text: str = "") -> None:
    if not sys.stdout:
        return
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode(), flush=True)


def cleanup_options_from(settings: Settings):
    from core.markdown_cleaner import CleanupOptions

    return CleanupOptions(i2c=settings.cleanup_i2c, page_artifacts=settings.cleanup_page_artifacts,
                          captions=settings.cleanup_captions, whitespace=settings.cleanup_whitespace)


def batch_options_from(settings: Settings, info, *, output_root=None, ocr=False, device_setting=None,
                       llm_cleanup=None, debug=None, page_range=None, stop_on_error=False):
    """BatchOptions for GUI and CLI (device resolved here, once per batch)."""
    from core.batch import BatchOptions
    from core.gpu import resolve_device

    device, why = resolve_device(device_setting or settings.device, info, settings.gpu_verified)
    opts = BatchOptions(
        output_root=output_root, ocr=bool(ocr), device=device,
        llm_cleanup=settings.llm_cleanup if llm_cleanup is None else llm_cleanup,
        cleanup_options=cleanup_options_from(settings),
        debug=settings.debug_mode if debug is None else debug,
        artifacts_setting=settings.artifacts_path, page_range=page_range,
        num_threads=settings.num_threads or None, recycle_mb=settings.worker_recycle_mb or None,
        stop_on_error=stop_on_error)
    return opts, why


def run_cli(args) -> int:
    """Convert files/folders without a window; prints progress. Returns the process exit code."""
    from core.batch import BatchQueue, BatchRunner, batch_summary
    from core.docling_runner import DOCLING_NOT_FOUND_MESSAGE, detect_docling
    from main import EXIT_ALL_FAILED, EXIT_CANCELLED, EXIT_NO_ENGINE, EXIT_NO_INPUT, EXIT_OK, EXIT_SOME_FAILED

    settings = Settings.load()
    quiet = bool(args.quiet)
    _say(f"{APP_NAME} {APP_VERSION}")
    queue = BatchQueue()  # the CLI never touches the GUI's saved list
    added = queue.add_paths(args.inputs, recursive=args.recursive)
    for m in added.missing:
        _say(f"Not found: {m}")
    for n in added.not_pdf:
        _say(f"Not a PDF (ignored): {n}")
    for d in added.duplicates:
        _say(f"Duplicate (ignored): {d}")
    if not queue.jobs:
        _say("No PDF files to convert.")
        return EXIT_NO_INPUT

    info = detect_docling(settings.docling_path, backend=settings.engine_backend)
    if not info.available:
        _say(DOCLING_NOT_FOUND_MESSAGE)
        _say(info.diagnostics())
        return EXIT_NO_ENGINE
    options, device_note = batch_options_from(
        settings, info, output_root=Path(args.out) if args.out else None, ocr=args.ocr, device_setting=args.device,
        llm_cleanup=not args.no_cleanup, debug=args.debug, page_range=args.pages,
        stop_on_error=args.stop_on_error)
    total = len(queue.jobs)
    _say(f"Docling {info.version} ({info.backend}); {total} PDF(s); output: {options.output_root or 'beside each PDF'}; "
         f"OCR: {'on' if options.ocr else 'off'}; device: {device_note}")
    index = {j.id: n for n, j in enumerate(queue.jobs, start=1)}
    announced = set()

    def emit(kind, payload):
        if kind == "log":
            _job_id, ev = payload
            if quiet:
                return
            if ev.kind == "stage":
                _say(f"   == {ev.text}")
            elif ev.level != "debug" or args.debug:
                _say(f"      {ev.text}")
        elif kind == "job":
            job = payload
            n = index.get(job["id"], "?")
            if job["status"] == "running":
                if job["id"] not in announced:
                    announced.add(job["id"])
                    _say(f"\n[{n}/{total}] {job['source']}")
            elif job["status"] != "pending":
                label = job["status"].replace("_", " ").upper()
                extra = job.get("markdown_path") or (job.get("error") or "").splitlines()[:1]
                extra = extra[0] if isinstance(extra, list) and extra else (extra or "")
                _say(f"[{n}/{total}] {label} ({job.get('elapsed_s') or 0:.0f}s): {extra}")
                for w in job.get("warnings", [])[:5]:
                    _say(f"      ! {w}")
                if job["status"] in ("failed", "partial") and job.get("log_path"):
                    _say(f"      log: {job['log_path']}")

    runner = BatchRunner(queue, info, options, emit)
    cancelled = False
    runner.start()
    try:
        while runner.is_alive():
            runner.join(0.5)
    except KeyboardInterrupt:
        cancelled = True
        _say("\nCancelling (Ctrl+C)...")
        runner.stop()
        while runner.is_alive():
            try:
                runner.join(0.5)
            except KeyboardInterrupt:
                pass
    summary = batch_summary(queue.snapshot())
    summary["duration_s"] = round(sum(j.get("duration_s") or 0 for j in summary["jobs"]), 1)
    _say("")
    _say(f"Summary: {summary['succeeded']} converted, {summary['failed']} failed/incomplete, "
         f"{summary['cancelled']} cancelled, {summary['pending']} not run ({summary['duration_s']:.0f}s)")
    if args.report:
        try:
            Path(args.report).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            _say(f"Batch summary: {args.report}")
        except OSError as exc:
            _say(f"Could not write the batch summary: {exc}")
    if cancelled or (summary["cancelled"] and not summary["succeeded"] and not summary["failed"]):
        return EXIT_CANCELLED
    if summary["failed"] == 0 and summary["pending"] == 0:
        return EXIT_OK
    if summary["succeeded"] == 0:
        return EXIT_ALL_FAILED
    return EXIT_SOME_FAILED


def run_self_test() -> int:
    """Print what the app can see (works from source and from the packaged EXE)."""
    from core.config import default_models_dir, resolve_artifacts_path
    from core.docling_runner import detect_docling, worker_script_path
    from core.gpu import gpu_status, resolve_device

    settings = Settings.load()
    frozen = getattr(sys, "frozen", False)
    _say(f"{APP_NAME} {APP_VERSION} self-test ({'packaged EXE' if frozen else 'source'}: {sys.executable})")
    script = worker_script_path()
    _say(f"Engine worker script: {script} ({'present' if script.is_file() else 'MISSING'})")
    started = time.monotonic()
    info = detect_docling(settings.docling_path, backend=settings.engine_backend)
    _say(info.diagnostics())
    _say(f"Detection took {time.monotonic() - started:.1f}s")
    models = resolve_artifacts_path(settings.artifacts_path)
    _say(f"Docling models: {models or f'not found in {default_models_dir()} (Docling uses its own cache or downloads on first use)'}")
    st = gpu_status(info)
    _say(f"GPU: {st.gpu.name + ' - ' if st.gpu else ''}{st.reason}")
    _say(f"Device that would be used: {resolve_device(settings.device, info, settings.gpu_verified, st)[1]}")
    ok = info.available and script.is_file()
    _say("SELF-TEST: OK" if ok else "SELF-TEST: PROBLEMS FOUND")
    return 0 if ok else 3


def _set_app_user_model_id() -> None:
    """Give the window its own taskbar identity/icon instead of python.exe's."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.Desktop.2")
    except Exception:
        pass
