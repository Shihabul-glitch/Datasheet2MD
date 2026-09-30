"""Main window. GUI only: all conversion work lives in ``core`` and runs on worker threads."""

from __future__ import annotations

import os
import platform
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

from core.config import (
    APP_NAME,
    APP_SUBTITLE,
    APP_VERSION,
    Settings,
    logs_dir,
    resolve_artifacts_path,
    resource_path,
    settings_path,
)
from core.converter import ConversionRequest, ConversionResult, Converter, UiEvent
from core.docling_runner import DOCLING_NOT_FOUND_MESSAGE, DoclingInfo, detect_docling
from core.output_manager import ExistsPolicy, base_component_dir, markdown_filename, output_root_for

try:  # optional drag-and-drop support
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
    _Base = type("_Base", (ctk.CTk, TkinterDnD.DnDWrapper), {})
except Exception:  # pragma: no cover - depends on installation
    _DND_AVAILABLE = False
    _Base = ctk.CTk

MONO = ("Consolas", 12)
LEVEL_COLORS = {
    "warning": ("#b45309", "#fbbf24"),
    "error": ("#b91c1c", "#f87171"),
    "success": ("#15803d", "#4ade80"),
    "debug": ("#6b7280", "#9ca3af"),
    "stage": ("#1d4ed8", "#93c5fd"),
}


def _icon_path() -> Optional[str]:
    p = resource_path("assets/app.ico")
    return str(p) if p.is_file() else None


def _apply_icon(window: tk.Misc) -> None:
    icon = _icon_path()
    if icon and os.name == "nt":
        try:
            window.iconbitmap(icon)
        except tk.TclError:
            pass


class ExistsDialog(ctk.CTkToplevel):
    """Ask what to do when the component folder already exists."""

    def __init__(self, master, folder: Path, new_folder: Path):
        super().__init__(master)
        self.title("Output folder exists")
        self.resizable(False, False)
        self.result = ExistsPolicy.CANCEL
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        msg = (f"The output folder already exists:\n\n{folder}\n\n"
               f"• Create new version → {new_folder.name}  (recommended)\n"
               f"• Overwrite generated files → replaces the .md file and images/ in "
               f"{folder.name} (other files are kept)")
        ctk.CTkLabel(self, text=msg, justify="left", wraplength=520).grid(
            row=0, column=0, padx=20, pady=(18, 12), sticky="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, padx=20, pady=(0, 18), sticky="e")
        new_btn = ctk.CTkButton(row, text="Create new version", width=160,
                                command=lambda: self._close(ExistsPolicy.NEW_VERSION))
        new_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Overwrite generated files", width=180, fg_color="#b45309",
                      hover_color="#92400e",
                      command=lambda: self._close(ExistsPolicy.OVERWRITE)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Cancel", width=90, fg_color="gray40", hover_color="gray30",
                      command=lambda: self._close(ExistsPolicy.CANCEL)).pack(side="left")
        self.bind("<Return>", lambda e: self._close(ExistsPolicy.NEW_VERSION))
        self.bind("<Escape>", lambda e: self._close(ExistsPolicy.CANCEL))
        self.protocol("WM_DELETE_WINDOW", lambda: self._close(ExistsPolicy.CANCEL))
        self.after(250, lambda: _apply_icon(self))
        self.after(50, self._activate)
        new_btn.focus_set()

    def _activate(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    def _close(self, policy: ExistsPolicy):
        self.result = policy
        self.grab_release()
        self.destroy()


class TextDialog(ctk.CTkToplevel):
    """Read-only, copyable text (diagnostics / error details)."""

    def __init__(self, master, title: str, text: str, extra_button=None):
        super().__init__(master)
        self.title(title)
        self.geometry("720x460")
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        box = ctk.CTkTextbox(self, font=MONO, wrap="word")
        box.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="nsew")
        box.insert("1.0", text)
        box.configure(state="disabled")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="e")
        if extra_button:
            label, cmd = extra_button
            ctk.CTkButton(row, text=label, width=130, command=lambda: (self.destroy(), cmd())).pack(
                side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Copy to clipboard", width=150,
                      command=lambda: self._copy(text)).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Close", width=90, command=self.destroy).pack(side="left")
        self.after(250, lambda: _apply_icon(self))
        self.after(50, self.lift)

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)


class MainWindow(_Base):
    def __init__(self, settings: Settings, initial_pdf: Optional[str] = None):
        super().__init__()
        self.settings = settings
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._docling: Optional[DoclingInfo] = None
        self._converter: Optional[Converter] = None
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._run_started = 0.0
        self._stage = ""
        self._substage = ""
        self._last_result: Optional[ConversionResult] = None
        self._closing = False

        self.title(f"{APP_NAME}")
        self.geometry("760x860")
        self.minsize(660, 720)
        _apply_icon(self)

        self._build()
        self._enable_dnd()
        if initial_pdf:
            self._set_entry(self.pdf_entry, initial_pdf)
            self._update_preview()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)
        self.after(1000, self._tick)
        self._start_detection()

    # ------------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 18}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(8, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(14, 4), **pad)
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=APP_NAME, font=ctk.CTkFont(size=24, weight="bold")).grid(row=0, column=0)
        ctk.CTkLabel(head, text=APP_SUBTITLE, text_color=("gray35", "gray70")).grid(row=1, column=0)
        self.docling_label = ctk.CTkLabel(head, text="Checking Docling…", text_color=("gray35", "gray65"),
                                          font=ctk.CTkFont(size=12))
        self.docling_label.grid(row=2, column=0, pady=(2, 0))

        # PDF file
        files = ctk.CTkFrame(self)
        files.grid(row=1, column=0, sticky="ew", pady=(8, 6), **pad)
        files.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(files, text="PDF File", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        # No textvariable: CTkEntry only shows placeholders on entries without one.
        self.pdf_entry = ctk.CTkEntry(files, placeholder_text="Select or drop a PDF datasheet…")
        self.pdf_entry.grid(row=1, column=0, sticky="ew", padx=(12, 6), pady=(4, 6))
        self.pdf_btn = ctk.CTkButton(files, text="Browse", width=90, command=self._browse_pdf)
        self.pdf_btn.grid(row=1, column=1, padx=(0, 12), pady=(4, 6))

        ctk.CTkLabel(files, text="Output", font=ctk.CTkFont(weight="bold")).grid(
            row=2, column=0, sticky="w", padx=12, pady=(4, 0))
        self.out_entry = ctk.CTkEntry(files, placeholder_text="Same directory as source PDF")
        self._set_entry(self.out_entry, self.settings.last_output_dir)
        self.out_entry.grid(row=3, column=0, sticky="ew", padx=(12, 6), pady=(4, 2))
        out_btns = ctk.CTkFrame(files, fg_color="transparent")
        out_btns.grid(row=3, column=1, padx=(0, 12), pady=(4, 2))
        self.out_btn = ctk.CTkButton(out_btns, text="Browse", width=90, command=self._browse_output)
        self.out_btn.pack()
        self.same_btn = ctk.CTkButton(files, text="Use PDF folder", width=90, height=22,
                                      fg_color="transparent", border_width=1,
                                      text_color=("gray20", "gray85"), command=self._use_pdf_folder)
        self.same_btn.grid(row=4, column=1, padx=(0, 12), pady=(0, 8))
        self.preview_label = ctk.CTkLabel(files, text="", anchor="w", justify="left",
                                          text_color=("gray35", "gray65"), font=ctk.CTkFont(size=12))
        self.preview_label.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 8))

        # options
        opts = ctk.CTkFrame(self)
        opts.grid(row=2, column=0, sticky="ew", pady=6, **pad)
        ctk.CTkLabel(opts, text="Conversion Options", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 4))
        self.cleanup_var = tk.BooleanVar(value=self.settings.llm_cleanup)
        self.ocr_var = tk.BooleanVar(value=self.settings.ocr_enabled)
        self.debug_var = tk.BooleanVar(value=self.settings.debug_mode)
        self.cleanup_cb = ctk.CTkCheckBox(opts, text="LLM-friendly cleanup", variable=self.cleanup_var,
                                          command=self._save_settings)
        self.cleanup_cb.grid(row=1, column=0, sticky="w", padx=12, pady=(2, 10))
        self.ocr_cb = ctk.CTkCheckBox(opts, text="Scanned PDF / Enable OCR", variable=self.ocr_var,
                                      command=self._save_settings)
        self.ocr_cb.grid(row=1, column=1, sticky="w", padx=12, pady=(2, 10))
        self.debug_cb = ctk.CTkCheckBox(opts, text="Debug mode", variable=self.debug_var,
                                        command=self._save_settings)
        self.debug_cb.grid(row=1, column=2, sticky="w", padx=12, pady=(2, 10))

        # convert / cancel
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, pady=(8, 4), **pad)
        self.convert_btn = ctk.CTkButton(actions, text="CONVERT", width=220, height=40,
                                         font=ctk.CTkFont(size=15, weight="bold"), command=self._convert,
                                         state="disabled")
        self.convert_btn.pack(side="left", padx=(0, 10))
        self.cancel_btn = ctk.CTkButton(actions, text="Cancel", width=100, height=40, state="disabled",
                                        fg_color="gray40", hover_color="gray30", command=self._cancel)
        self.cancel_btn.pack(side="left")

        # status
        self.status_label = ctk.CTkLabel(self, text="Status: Ready", anchor="w", font=ctk.CTkFont(size=13))
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(6, 2), **pad)
        self.progress = ctk.CTkProgressBar(self, mode="determinate")
        self.progress.set(0)
        self.progress.grid(row=5, column=0, sticky="ew", pady=(0, 8), **pad)

        # log / validation
        self.tabs = ctk.CTkTabview(self, height=260)
        self.tabs.grid(row=8, column=0, sticky="nsew", pady=(0, 6), **pad)
        self.tabs.add("Log")
        self.tabs.add("Validation")
        for name in ("Log", "Validation"):
            self.tabs.tab(name).grid_columnconfigure(0, weight=1)
            self.tabs.tab(name).grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(self.tabs.tab("Log"), font=MONO, wrap="word")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.val_box = ctk.CTkTextbox(self.tabs.tab("Validation"), font=ctk.CTkFont(family="Segoe UI", size=14),
                                      wrap="word")
        self.val_box.grid(row=0, column=0, sticky="nsew")
        for box in (self.log_box, self.val_box):
            for level, (light, dark) in LEVEL_COLORS.items():
                box.tag_config(level, foreground=light if ctk.get_appearance_mode() == "Light" else dark)
            box.configure(state="disabled")
        self._set_validation(["Validation results appear here after a conversion."])

        # bottom buttons
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=9, column=0, pady=(2, 14), **pad)
        self.open_btn = ctk.CTkButton(bottom, text="Open Output Folder", width=170, state="disabled",
                                      command=self._open_output)
        self.open_btn.pack(side="left", padx=(0, 8))
        self.open_md_btn = ctk.CTkButton(bottom, text="Open Markdown", width=130, state="disabled",
                                         command=self._open_markdown)
        self.open_md_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(bottom, text="Copy Log", width=100, fg_color="gray40", hover_color="gray30",
                      command=self._copy_log).pack(side="left", padx=(0, 8))
        ctk.CTkButton(bottom, text="Diagnostics", width=110, fg_color="gray40", hover_color="gray30",
                      command=self._show_diagnostics).pack(side="left")

        for entry in (self.pdf_entry, self.out_entry):
            for seq in ("<KeyRelease>", "<FocusOut>", "<<Paste>>"):
                entry.bind(seq, lambda e: self.after_idle(self._update_preview), add="+")
        self._update_preview()

    # ------------------------------------------------------------------ entry helpers
    @staticmethod
    def _set_entry(entry: ctk.CTkEntry, text: str) -> None:
        previous = entry.cget("state")
        entry.configure(state="normal")
        if text:
            entry.delete(0, "end")
            entry.insert(0, text)
        elif entry.get():
            # CTkEntry starts with _is_focused=True, so delete() alone would leave the
            # field blank instead of showing the placeholder; restore it explicitly.
            entry.delete(0, "end")
            restore = getattr(entry, "_activate_placeholder", None)
            if restore:
                restore()
        entry.configure(state=previous)

    def _pdf_text(self) -> str:
        return self.pdf_entry.get().strip().strip('"')

    def _out_text(self) -> str:
        return self.out_entry.get().strip().strip('"')

    def _use_pdf_folder(self):
        self._set_entry(self.out_entry, "")
        self.focus_set()  # lets the entry show its placeholder again
        self._update_preview()
        self._save_settings()

    def _enable_dnd(self):
        self._dnd = False
        if not _DND_AVAILABLE:
            return
        try:
            self.TkdndVersion = TkinterDnD._require(self)
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
            self._dnd = True
            self.pdf_entry.configure(placeholder_text="Select or drag & drop a PDF datasheet here…")
        except Exception:
            self._dnd = False

    # ------------------------------------------------------------------ helpers
    def _log(self, text: str, level: str = "info", prefix: str = "> "):
        self.log_box.configure(state="normal")
        tag = level if level in LEVEL_COLORS else None
        self.log_box.insert("end", f"{prefix}{text}\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_validation(self, lines, levels=None):
        self.val_box.configure(state="normal")
        self.val_box.delete("1.0", "end")
        for i, line in enumerate(lines):
            level = levels[i] if levels else None
            self.val_box.insert("end", line + "\n", level)
        self.val_box.configure(state="disabled")

    def _set_status(self, text: str, level: Optional[str] = None):
        color = None
        if level in LEVEL_COLORS:
            color = LEVEL_COLORS[level]
        self.status_label.configure(text=f"Status: {text}",
                                    text_color=color if color else ("gray10", "gray90"))

    def _update_preview(self):
        pdf = self._pdf_text()
        if not pdf:
            self.preview_label.configure(text="")
            return
        try:
            root = output_root_for(pdf, self._out_text() or None)
            comp = base_component_dir(pdf, root)
            note = "  (exists)" if comp.exists() else ""
            self.preview_label.configure(text=f"→ {comp.name}{os.sep}{markdown_filename(pdf)} + images{os.sep}{note}")
        except Exception:
            self.preview_label.configure(text="")

    def _save_settings(self):
        self.settings.llm_cleanup = bool(self.cleanup_var.get())
        self.settings.ocr_enabled = bool(self.ocr_var.get())
        self.settings.debug_mode = bool(self.debug_var.get())
        self.settings.last_output_dir = self._out_text()
        self.settings.save()

    def _set_running(self, running: bool):
        self._running = running
        state = "disabled" if running else "normal"
        for w in (self.pdf_entry, self.pdf_btn, self.out_entry, self.out_btn, self.same_btn,
                  self.cleanup_cb, self.ocr_cb, self.debug_cb):
            w.configure(state=state)
        self.cancel_btn.configure(state="normal" if running else "disabled")
        can_convert = (not running) and bool(self._docling and self._docling.available)
        self.convert_btn.configure(state="normal" if can_convert else "disabled")
        if running:
            self.open_btn.configure(state="disabled")
            self.open_md_btn.configure(state="disabled")

    # ------------------------------------------------------------------ docling detection
    def _start_detection(self):
        self.docling_label.configure(text="Checking Docling installation…", text_color=("gray35", "gray65"))
        self.convert_btn.configure(state="disabled")
        explicit = self.settings.docling_path

        def work():
            try:
                info = detect_docling(explicit)
            except Exception as exc:  # never crash on detection
                info = DoclingInfo(False, error=f"{exc.__class__.__name__}: {exc}")
            self._events.put(("docling", info))

        threading.Thread(target=work, daemon=True).start()

    def _on_docling_detected(self, info: DoclingInfo):
        self._docling = info
        if info.available:
            models = resolve_artifacts_path(self.settings.artifacts_path)
            model_note = "local models" if models else "default model cache"
            self.docling_label.configure(text=f"✓ Docling {info.version} ready ({model_note})",
                                         text_color=LEVEL_COLORS["success"])
            self._log(f"Docling {info.version} found: {info.path}", "success")
            if not self._running:
                self.convert_btn.configure(state="normal")
        else:
            self.docling_label.configure(text="✗ Docling was not found", text_color=LEVEL_COLORS["error"])
            self._log("Docling was not found. Please install Docling or add it to your PATH.", "error")
            if messagebox.askyesno(
                "Docling not found",
                DOCLING_NOT_FOUND_MESSAGE + "\n\nShow diagnostic details?",
                icon="error", parent=self,
            ):
                self._show_diagnostics()

    # ------------------------------------------------------------------ actions
    def _browse_pdf(self):
        initial = self.settings.last_input_dir or str(Path.home())
        path = filedialog.askopenfilename(
            parent=self, title="Select a PDF datasheet", initialdir=initial,
            filetypes=[("PDF datasheets", "*.pdf"), ("All files", "*.*")])
        if path:
            self._set_pdf(path)

    def _set_pdf(self, path: str):
        p = Path(path)
        self._set_entry(self.pdf_entry, str(p))
        self._update_preview()
        self.settings.last_input_dir = str(p.parent)
        self._save_settings()
        self._set_status("Ready")
        self.progress.configure(mode="determinate")
        self.progress.set(0)

    def _browse_output(self):
        initial = self._out_text() or self.settings.last_output_dir or self.settings.last_input_dir
        path = filedialog.askdirectory(parent=self, title="Select output folder",
                                       initialdir=initial or str(Path.home()))
        if path:
            self._set_entry(self.out_entry, str(Path(path)))
            self._update_preview()
            self._save_settings()

    def _on_drop(self, event):
        if self._running:
            return
        try:
            items = self.tk.splitlist(event.data)
        except tk.TclError:
            items = [event.data]
        pdfs = [i for i in items if i.lower().endswith(".pdf")]
        if pdfs:
            self._set_pdf(pdfs[0])
            if len(pdfs) > 1:
                self._log("Several PDFs were dropped; only the first one is used.", "warning")
        else:
            self._log("Dropped item is not a PDF file.", "warning")

    def _convert(self):
        if self._running:
            return
        if not (self._docling and self._docling.available):
            messagebox.showerror("Docling not found", DOCLING_NOT_FOUND_MESSAGE, parent=self)
            return
        pdf_text = self._pdf_text()
        if not pdf_text:
            messagebox.showwarning(APP_NAME, "Please select a PDF file first.", parent=self)
            return
        pdf = Path(pdf_text)
        if not pdf.is_file():
            messagebox.showerror("Conversion failed", f"The PDF file was not found:\n\n{pdf}", parent=self)
            return
        out_text = self._out_text()
        output_root = Path(out_text) if out_text else None
        if output_root is not None and output_root.exists() and not output_root.is_dir():
            messagebox.showerror("Conversion failed", f"The output location is not a folder:\n\n{output_root}",
                                 parent=self)
            return

        policy = ExistsPolicy.NEW_VERSION
        try:
            existing = base_component_dir(pdf, output_root)
        except Exception as exc:
            messagebox.showerror("Conversion failed", f"Invalid output location:\n\n{exc}", parent=self)
            return
        if existing.exists():
            from core.output_manager import next_version_dir

            dlg = ExistsDialog(self, existing, next_version_dir(existing))
            self.wait_window(dlg)
            policy = dlg.result
            if policy is ExistsPolicy.CANCEL:
                self._log("Conversion cancelled (output folder exists).", "warning")
                return

        self._save_settings()
        request = ConversionRequest(
            pdf_path=pdf,
            output_root=output_root,
            policy=policy,
            ocr=bool(self.ocr_var.get()),
            llm_cleanup=bool(self.cleanup_var.get()),
            debug=bool(self.debug_var.get()),
            artifacts_setting=self.settings.artifacts_path,
        )
        self._last_result = None
        self._set_validation(["Conversion in progress…"])
        self.tabs.set("Log")
        self._log("", prefix="")
        self._log(f"Converting {pdf.name}", "stage", prefix="=== ")
        self._set_running(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._run_started = time.monotonic()
        self._stage, self._substage = "Preparing", ""
        self._set_status("Preparing")

        self._converter = Converter(request, self._docling.path, lambda ev: self._events.put(("event", ev)))

        def work():
            try:
                result = self._converter.run()
            except Exception as exc:  # Converter.run already catches; this is a safety net
                result = ConversionResult(error_title="Conversion failed", error_message=str(exc))
            self._events.put(("done", result))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _cancel(self):
        if self._running and self._converter:
            self._set_status("Cancelling…", "warning")
            self.cancel_btn.configure(state="disabled")
            self._log("Cancelling…", "warning")
            threading.Thread(target=self._converter.cancel, daemon=True).start()

    # ------------------------------------------------------------------ events
    def _poll_events(self):
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "event":
                    self._handle_event(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "docling":
                    self._on_docling_detected(payload)
        except queue.Empty:
            pass
        if not self._closing:
            self.after(100, self._poll_events)

    def _handle_event(self, ev: UiEvent):
        if ev.kind == "stage":
            self._stage, self._substage = ev.text, ""
            self._log(f"{ev.text}…" if ev.text != "Completed" else "Completed.", "stage")
            self._refresh_status()
        elif ev.kind == "substage":
            self._substage = ev.text
            self._refresh_status()
        elif ev.kind == "log":
            if ev.level == "debug" and not self.debug_var.get():
                return
            self._log(ev.text, ev.level, prefix="  ")

    def _refresh_status(self):
        if not self._running:
            return
        elapsed = int(time.monotonic() - self._run_started)
        text = self._stage + (f" — {self._substage}" if self._substage else "")
        self._set_status(f"{text}…  ({elapsed // 60:d}:{elapsed % 60:02d})")

    def _tick(self):
        if self._running:
            self._refresh_status()
        if not self._closing:
            self.after(1000, self._tick)

    def _on_done(self, result: ConversionResult):
        self._last_result = result
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self._set_running(False)
        self._worker = None
        if result.validation:
            report = result.validation
            lines = report.lines()
            levels = [{"ok": "success", "warn": "warning", "fail": "error"}[c.status] for c in report.checks]
            self._set_validation(lines, levels)
            for c in report.checks:
                self._log(c.line(), {"ok": "success", "warn": "warning", "fail": "error"}[c.status], prefix="  ")
        if result.success:
            self.progress.set(1)
            mins, secs = divmod(int(result.seconds), 60)
            self._set_status(f"Conversion completed successfully ({mins}:{secs:02d})", "success")
            self.open_btn.configure(state="normal")
            self.open_md_btn.configure(state="normal")
            self.tabs.set("Validation")
            self._update_preview()
            plan = result.plan
            messagebox.showinfo(
                APP_NAME,
                "Conversion completed successfully\n\n"
                f"{plan.markdown_path}\n"
                f"{result.validation.image_count if result.validation else 0} image(s) in "
                f"{plan.images_dir.name}{os.sep}",
                parent=self,
            )
        elif result.cancelled:
            self.progress.set(0)
            self._set_status("Cancelled", "warning")
            self._set_validation(["Conversion was cancelled."], ["warning"])
        else:
            self.progress.set(0)
            self._set_status("Conversion failed", "error")
            if not result.validation:
                self._set_validation([f"✗ Conversion failed: {result.error_message}"], ["error"])
            if result.plan and result.plan.component_dir.is_dir():
                self.open_btn.configure(state="normal")
            log_note = f"\n\nDetailed log:\n{result.log_path}" if result.log_path else ""
            if messagebox.askyesno(
                result.error_title or "Conversion failed",
                f"Conversion failed\n\n{result.error_message}{log_note}\n\nShow the detailed log?",
                icon="error", parent=self,
            ):
                self._show_log_file(result.log_path)

    # ------------------------------------------------------------------ misc buttons
    def _open_output(self):
        r = self._last_result
        target = r.output_dir if r and r.output_dir else None
        if target and target.is_dir():
            os.startfile(str(target))
        else:
            messagebox.showwarning(APP_NAME, "The output folder no longer exists.", parent=self)

    def _open_markdown(self):
        r = self._last_result
        if r and r.plan and r.plan.markdown_path.is_file():
            os.startfile(str(r.plan.markdown_path))

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_box.get("1.0", "end"))
        self._set_status("Log copied to clipboard")

    def _show_log_file(self, path: Optional[Path]):
        text = ""
        if path and path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[-60000:]
            except OSError as exc:
                text = f"Could not read log: {exc}"
        TextDialog(self, "Conversion log", text or self.log_box.get("1.0", "end"))

    def _show_diagnostics(self):
        info = self._docling
        models = resolve_artifacts_path(self.settings.artifacts_path)
        lines = [
            f"{APP_NAME} {APP_VERSION}",
            f"Python {platform.python_version()} ({'frozen executable' if getattr(sys, 'frozen', False) else sys.executable})",
            f"Windows: {platform.platform()}",
            f"Drag & drop: {'enabled' if getattr(self, '_dnd', False) else 'not available'}",
            f"Settings: {settings_path()}",
            f"Logs: {logs_dir()}",
            f"Docling models (--artifacts-path): {models or 'not passed (Docling uses its default cache)'}",
            "",
            info.diagnostics() if info else "Docling detection has not finished yet.",
        ]
        TextDialog(self, "Diagnostics", "\n".join(lines), extra_button=("Retry detection", self._start_detection))

    def _on_close(self):
        if self._running:
            if not messagebox.askyesno(APP_NAME, "A conversion is running. Cancel it and exit?", parent=self):
                return
            if self._converter:
                self._converter.cancel()
            if self._worker:
                self._worker.join(timeout=15)
        self._save_settings()
        self._closing = True
        self.destroy()

    def report_callback_exception(self, exc, val, tb):  # Tk callback errors -> dialog, not crash
        import traceback

        detail = "".join(traceback.format_exception(exc, val, tb))
        try:
            logs_dir().mkdir(parents=True, exist_ok=True)
            (logs_dir() / "gui_errors.log").open("a", encoding="utf-8").write(detail + "\n")
        except OSError:
            pass
        TextDialog(self, "Unexpected error", detail)
