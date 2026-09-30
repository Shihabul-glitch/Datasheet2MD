"""Application bootstrap: GUI mode and headless command-line mode."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from core.config import APP_NAME, Settings


def run_gui(initial_pdf: Optional[str] = None) -> int:
    import customtkinter as ctk

    from ui.main_window import MainWindow

    _set_app_user_model_id()
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    window = MainWindow(Settings.load(), initial_pdf=initial_pdf)
    window.mainloop()
    return 0


def run_cli(pdf: str, output: Optional[str], ocr: bool, cleanup: bool, debug: bool,
            overwrite: bool, pages: Optional[str]) -> int:
    """Convert without a window; prints progress. Returns a process exit code."""
    from core.converter import ConversionRequest, Converter
    from core.docling_runner import DOCLING_NOT_FOUND_MESSAGE, detect_docling
    from core.output_manager import ExistsPolicy

    settings = Settings.load()
    out = sys.stdout if sys.stdout else None

    def say(text: str) -> None:
        if out:
            try:
                print(text, flush=True)
            except UnicodeEncodeError:
                print(text.encode("ascii", "replace").decode(), flush=True)

    info = detect_docling(settings.docling_path)
    if not info.available:
        say(DOCLING_NOT_FOUND_MESSAGE)
        say(info.diagnostics())
        return 3
    say(f"Docling {info.version}: {info.path}")
    request = ConversionRequest(
        pdf_path=Path(pdf),
        output_root=Path(output) if output else None,
        policy=ExistsPolicy.OVERWRITE if overwrite else ExistsPolicy.NEW_VERSION,
        ocr=ocr,
        llm_cleanup=cleanup,
        debug=debug,
        artifacts_setting=settings.artifacts_path,
        page_range=pages,
    )

    def emit(ev):
        if ev.kind == "stage":
            say(f"== {ev.text}")
        elif ev.kind == "log" and (ev.level != "debug" or debug):
            say(f"   {ev.text}")

    result = Converter(request, info.path, emit).run()
    if result.validation:
        say("")
        for line in result.validation.lines():
            say(line)
    if result.success:
        say(f"\nConversion completed successfully: {result.plan.markdown_path}")
        return 0
    say(f"\n{result.error_title or 'Conversion failed'}: {result.error_message}")
    if result.log_path:
        say(f"Log: {result.log_path}")
    return 2 if result.cancelled else 1


def _set_app_user_model_id() -> None:
    """Give the window its own taskbar identity/icon instead of python.exe's."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.Desktop.1")
    except Exception:
        pass
