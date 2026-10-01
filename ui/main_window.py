"""Main window. GUI only: conversion work lives in ``core`` and runs on a worker thread.

Thread rule: background threads never touch Tk widgets. They put events on
``self._events``; ``_poll_events`` (Tk thread, every 100 ms) applies them.
"""

from __future__ import annotations

import collections
import datetime as _dt
import os
import platform
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Deque, Dict, List, Optional

import customtkinter as ctk

from core.batch import RUNNING, BatchQueue, BatchRunner, Job
from core.config import (
    APP_NAME,
    APP_SUBTITLE,
    APP_VERSION,
    Settings,
    logs_dir,
    queue_state_path,
    resolve_artifacts_path,
    resource_path,
    settings_path,
)
from core.converter import CANCELLED, COMPLETED, FAILED, PARTIAL, WARNINGS
from core.docling_runner import DOCLING_NOT_FOUND_MESSAGE, DoclingInfo, detect_docling

try:  # optional drag-and-drop support
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
    _Base = type("_Base", (ctk.CTk, TkinterDnD.DnDWrapper), {})
except Exception:  # pragma: no cover - depends on installation
    _DND_AVAILABLE = False
    _Base = ctk.CTk

MONO = ("Consolas", 11)
ACCENT = "#1f6aa5"
LEVEL_COLORS = {"warning": "#fbbf24", "error": "#f87171", "success": "#4ade80", "debug": "#9ca3af",
                "stage": "#93c5fd", "info": "#dce4ee"}
STATUS_COLORS = {"pending": "#c9d1d9", RUNNING: "#93c5fd", COMPLETED: "#4ade80", WARNINGS: "#fbbf24",
                 PARTIAL: "#fb923c", FAILED: "#f87171", CANCELLED: "#9ca3af", "interrupted": "#fb923c"}
JOB_LOG_LINES = 3000
BATCH_LOG_LINES = 5000
GRAY_BTN = {"fg_color": "gray35", "hover_color": "gray28"}


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


def _fmt_secs(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:d}:{s % 60:02d}"


def _parse_iso(text: Optional[str]) -> Optional[float]:
    try:
        return _dt.datetime.fromisoformat(text).timestamp() if text else None
    except ValueError:
        return None


class TextDialog(ctk.CTkToplevel):
    """Read-only, copyable text."""

    def __init__(self, master, title: str, text: str):
        super().__init__(master)
        self.title(title)
        self.geometry("760x480")
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        box = ctk.CTkTextbox(self, font=MONO, wrap="word")
        box.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="nsew")
        box.insert("1.0", text)
        box.configure(state="disabled")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="e")
        ctk.CTkButton(row, text="Copy to clipboard", width=150,
                      command=lambda: (self.clipboard_clear(), self.clipboard_append(text))).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row, text="Close", width=90, command=self.destroy).pack(side="left")
        self.after(250, lambda: _apply_icon(self))
        self.after(50, self.lift)


class SettingsDialog(ctk.CTkToplevel):
    """Technical settings that are not part of the everyday workflow."""

    def __init__(self, master: "MainWindow"):
        super().__init__(master)
        self.main = master
        self.title("Settings and diagnostics")
        self.geometry("780x560")
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)
        st = master.settings
        bold = ctk.CTkFont(weight="bold")

        dev = ctk.CTkFrame(self)
        dev.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        ctk.CTkLabel(dev, text="Processing device", font=bold).pack(side="left", padx=(12, 8), pady=8)
        self.device_menu = ctk.CTkOptionMenu(dev, width=110, values=["Auto", "CPU", "GPU"], command=self._save)
        self.device_menu.set(st.device.upper() if st.device != "auto" else "Auto")
        self.device_menu.pack(side="left", padx=(0, 8))
        self.gpu_btn = ctk.CTkButton(dev, text="Test GPU", width=100, command=self._test_gpu, **GRAY_BTN)
        self.gpu_btn.pack(side="left", padx=(0, 8))
        self.gpu_label = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=740, text_color="gray75")
        self.gpu_label.grid(row=1, column=0, sticky="ew", padx=16)

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=2, column=0, sticky="ew", padx=12, pady=6)
        self.cleanup_var = tk.BooleanVar(value=st.llm_cleanup)
        ctk.CTkCheckBox(opts, text="LLM-friendly cleanup (I²C notation, page numbers/footers, captions as alt text)",
                        variable=self.cleanup_var, command=self._save).pack(anchor="w", pady=2)
        self.debug_var = tk.BooleanVar(value=st.debug_mode)
        ctk.CTkCheckBox(opts, text="Debug: verbose Docling log and keep intermediate files",
                        variable=self.debug_var, command=self._save).pack(anchor="w", pady=2)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="w", padx=12, pady=(4, 4))
        ctk.CTkButton(btns, text="Retry Docling detection", width=180, command=self._retry, **GRAY_BTN).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Open log folder", width=130,
                      command=lambda: master._open(str(logs_dir()), "log folder"), **GRAY_BTN).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Copy diagnostics", width=130,
                      command=lambda: (self.clipboard_clear(), self.clipboard_append(self._diag_text()))).pack(side="left")
        ctk.CTkLabel(self, text="Diagnostics", font=bold, anchor="w").grid(row=4, column=0, sticky="w", padx=16, pady=(6, 0))
        self.box = ctk.CTkTextbox(self, font=MONO, wrap="word")
        self.box.grid(row=5, column=0, sticky="nsew", padx=12, pady=(2, 12))
        self._refresh()
        self.after(250, lambda: _apply_icon(self))
        self.after(50, self.lift)

    def _diag_text(self) -> str:
        m = self.main
        info = m._docling
        models = resolve_artifacts_path(m.settings.artifacts_path)
        lines = [
            f"{APP_NAME} {APP_VERSION}",
            f"Python {platform.python_version()} ({'packaged EXE: ' + sys.executable if getattr(sys, 'frozen', False) else sys.executable})",
            f"Windows: {platform.platform()}",
            f"Drag & drop: {'enabled' if getattr(m, '_dnd', False) else 'not available'}",
            f"Settings: {settings_path()}",
            f"File list: {queue_state_path()}",
            f"Logs: {logs_dir()}",
            f"Docling models: {models or 'not found in the default folder (Docling uses its own cache or downloads on first use)'}",
            "",
            info.diagnostics() if info else "Docling detection has not finished yet.",
        ]
        if m._gpu_status:
            lines += ["", f"GPU: {m._gpu_status.reason}"]
        return "\n".join(lines)

    def _refresh(self):
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("1.0", self._diag_text())
        self.box.configure(state="disabled")
        st = self.main._gpu_status
        from core.gpu import resolve_device

        dev = resolve_device(self.main.settings.device, self.main._docling, self.main.settings.gpu_verified, st)[1] \
            if self.main._docling else "Docling not detected yet"
        self.gpu_label.configure(text=f"Will use: {dev}" + (f"\n{st.reason}" if st else ""))
        usable = bool(st and st.usable)
        self.gpu_btn.configure(state="normal" if usable else "disabled")

    def _save(self, *_):
        st = self.main.settings
        st.device = {"Auto": "auto", "CPU": "cpu", "GPU": "gpu"}[self.device_menu.get()]
        st.llm_cleanup = bool(self.cleanup_var.get())
        st.debug_mode = bool(self.debug_var.get())
        st.save()
        self._refresh()

    def _retry(self):
        self.main._start_detection()
        self.after(1500, self._refresh)

    def _test_gpu(self):
        from core.gpu import signature, test_gpu

        info, st = self.main._docling, self.main._gpu_status
        self.gpu_btn.configure(state="disabled", text="Testing…")
        self.gpu_label.configure(text="Running a test conversion on the GPU (can take a minute)…")

        def work():
            ok, msg = test_gpu(info)
            self.main._events.put(("gpu_test", (ok, msg, signature(info, st) if ok else "")))

        threading.Thread(target=work, daemon=True).start()

    def gpu_test_done(self, ok: bool, msg: str):
        self.gpu_btn.configure(text="Test GPU")
        self._refresh()
        self.gpu_label.configure(text=self.gpu_label.cget("text") + f"\nTest: {msg}")


class MainWindow(_Base):
    def __init__(self, settings: Settings, initial_paths: Optional[List[str]] = None):
        super().__init__()
        self.settings = settings
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._docling: Optional[DoclingInfo] = None
        self._gpu_status = None
        self._detecting = False
        self._runner: Optional[BatchRunner] = None
        self._closing = False
        self._settings_dialog: Optional[SettingsDialog] = None
        self._job_logs: Dict[str, Deque[tuple]] = {}
        self._batch_log: Deque[tuple] = collections.deque(maxlen=BATCH_LOG_LINES)
        self._log_view_job: Optional[str] = "__none__"
        self._batch_started = 0.0
        self.queue = BatchQueue.load(queue_state_path())

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry(settings.window_geometry or "900x700")
        self.minsize(760, 560)
        _apply_icon(self)
        self._build()
        self._fit_to_content()
        self._enable_dnd()
        self._refresh_tree()
        if any(j.status == "interrupted" for j in self.queue.jobs):
            self._blog("Restored the file list from the last session; interrupted files are converted again "
                       "when you press Convert.", "stage")
        if initial_paths:
            self._add_paths(initial_paths)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)
        self.after(1000, self._tick)
        self._start_detection()

    # ================================================================ layout
    def _scale(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_window_scaling(self))
        except Exception:
            return 1.0

    def _build(self):
        pad = {"padx": 14}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        bold = ctk.CTkFont(weight="bold")

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(10, 2), **pad)
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text=APP_NAME, font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text=f"  {APP_SUBTITLE}", text_color="gray70").grid(row=0, column=1, sticky="w")
        ctk.CTkButton(head, text="Settings…", width=100, command=self._open_settings, **GRAY_BTN).grid(
            row=0, column=2, sticky="e")
        self.docling_label = ctk.CTkLabel(head, text="Checking Docling…", text_color="gray70", anchor="w")
        self.docling_label.grid(row=1, column=0, columnspan=3, sticky="w")

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 4), **pad)
        ctk.CTkButton(bar, text="Add PDFs…", width=110, command=self._browse_pdfs).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bar, text="Add Folder…", width=110, command=self._browse_folder).pack(side="left", padx=(0, 6))
        self.subdirs_var = tk.BooleanVar(value=self.settings.include_subfolders)
        ctk.CTkCheckBox(bar, text="Include subfolders", variable=self.subdirs_var,
                        command=self._save_settings).pack(side="left", padx=(4, 14))
        self.remove_btn = ctk.CTkButton(bar, text="Remove Selected", width=130, command=self._remove_selected,
                                        **GRAY_BTN)
        self.remove_btn.pack(side="left", padx=(0, 6))
        self.clear_btn = ctk.CTkButton(bar, text="Clear List", width=100, command=self._clear, **GRAY_BTN)
        self.clear_btn.pack(side="left")

        table = ctk.CTkFrame(self)
        table.grid(row=2, column=0, sticky="nsew", pady=(2, 6), **pad)
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        self._style_tree()
        self.tree = ttk.Treeview(table, columns=("file", "status", "time"), show="headings",
                                 selectmode="extended", style="D2M.Treeview")
        s = self._scale()
        for col, title, width, stretch, anchor in (("file", "File", 420, True, "w"),
                                                   ("status", "Status", 260, True, "w"),
                                                   ("time", "Time", 70, False, "center")):
            self.tree.heading(col, text=title, anchor=anchor)
            self.tree.column(col, width=int(width * s), minwidth=int(50 * s), stretch=stretch, anchor=anchor)
        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)
        ysb = ctk.CTkScrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        ysb.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=6)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<Double-1>", lambda e: self._open_output())
        self.empty_label = ctk.CTkLabel(table, text="Add PDFs or a folder to get started.", text_color="gray60")

        opts = ctk.CTkFrame(self)
        opts.grid(row=3, column=0, sticky="ew", pady=(0, 6), **pad)
        opts.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(opts, text="Output", font=bold).grid(row=0, column=0, sticky="w", padx=(12, 8), pady=(8, 4))
        self.beside_var = tk.StringVar(value="beside" if self.settings.output_beside_source else "root")
        self.beside_rb = ctk.CTkRadioButton(opts, text="Beside each source PDF", variable=self.beside_var,
                                            value="beside", command=self._on_output_mode)
        self.beside_rb.grid(row=0, column=1, sticky="w", pady=(8, 4))
        self.root_rb = ctk.CTkRadioButton(opts, text="Folder:", variable=self.beside_var, value="root",
                                          command=self._on_output_mode)
        self.root_rb.grid(row=0, column=2, sticky="w", padx=(10, 4), pady=(8, 4))
        self.out_entry = ctk.CTkEntry(opts, placeholder_text="Choose an output folder…")
        if self.settings.last_output_dir:
            self.out_entry.insert(0, self.settings.last_output_dir)
        self.out_entry.grid(row=0, column=3, sticky="ew", padx=(0, 6), pady=(8, 4))
        self.out_entry.bind("<FocusOut>", lambda e: self._save_settings())
        self.out_btn = ctk.CTkButton(opts, text="Browse…", width=90, command=self._browse_output)
        self.out_btn.grid(row=0, column=4, padx=(0, 12), pady=(8, 4))
        self.ocr_var = tk.BooleanVar(value=self.settings.ocr_enabled)
        self.ocr_cb = ctk.CTkCheckBox(opts, text="Scanned PDF / Enable OCR", variable=self.ocr_var,
                                      command=self._save_settings)
        self.ocr_cb.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 10))
        ctk.CTkLabel(opts, text="Only for scanned or image-based PDFs. Normal datasheets already contain text; "
                                "OCR makes conversion much slower.", text_color="gray65", anchor="w").grid(
            row=1, column=2, columnspan=3, sticky="w", pady=(4, 10))

        ctl = ctk.CTkFrame(self, fg_color="transparent")
        ctl.grid(row=4, column=0, sticky="ew", pady=(2, 2), **pad)
        ctl.grid_columnconfigure(3, weight=1)
        self.convert_btn = ctk.CTkButton(ctl, text="Convert", width=150, height=36,
                                         font=ctk.CTkFont(size=15, weight="bold"), command=self._convert)
        self.convert_btn.grid(row=0, column=0, padx=(0, 8))
        self.cancel_btn = ctk.CTkButton(ctl, text="Cancel Current", width=130, height=36,
                                        command=self._cancel_current, **GRAY_BTN)
        self.cancel_btn.grid(row=0, column=1, padx=(0, 8))
        self.stop_btn = ctk.CTkButton(ctl, text="Stop Batch", width=110, height=36, command=self._stop_batch,
                                      fg_color="#9a3412", hover_color="#7c2d12")
        self.stop_btn.grid(row=0, column=2, padx=(0, 8))
        self.overall_label = ctk.CTkLabel(ctl, text="", anchor="e")
        self.overall_label.grid(row=0, column=3, sticky="e")

        prog = ctk.CTkFrame(self, fg_color="transparent")
        prog.grid(row=5, column=0, sticky="ew", pady=(4, 2), **pad)
        prog.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(prog, text="Status: Checking Docling installation…", anchor="w",
                                         justify="left", wraplength=800)
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.stage_bar = ctk.CTkProgressBar(prog, mode="indeterminate", height=8, width=160)
        self.stage_bar.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.stage_bar.grid_remove()

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=6, column=0, sticky="ew", pady=(2, 6), **pad)
        self.open_btn = ctk.CTkButton(bottom, text="Open Output Folder", width=150, command=self._open_output)
        self.open_btn.pack(side="left", padx=(0, 6))
        self.open_md_btn = ctk.CTkButton(bottom, text="Open Markdown", width=130, command=self._open_markdown)
        self.open_md_btn.pack(side="left", padx=(0, 6))
        self.log_toggle = ctk.CTkButton(bottom, text="", width=110, command=self._toggle_log, **GRAY_BTN)
        self.log_toggle.pack(side="right")
        ctk.CTkButton(bottom, text="Copy Log", width=90, command=self._copy_log, **GRAY_BTN).pack(side="right", padx=(0, 6))

        self.log_box = ctk.CTkTextbox(self, font=MONO, wrap="word", height=170)
        for level, color in LEVEL_COLORS.items():
            self.log_box.tag_config(level, foreground=color)
        self.log_box.configure(state="disabled")
        self._apply_log_visibility()

        self.bind("<Control-o>", lambda e: self._browse_pdfs())
        self._on_output_mode(save=False)
        self._update_controls()

    def _fit_to_content(self):
        """Grow the window to what the widgets need at the current display scaling (never beyond the screen)."""
        try:
            self.update_idletasks()
            req_w, req_h = self.winfo_reqwidth(), self.winfo_reqheight()
            scr_w, scr_h = self.winfo_screenwidth(), self.winfo_screenheight()
            cur_w, cur_h = self.winfo_width(), self.winfo_height()
            w = min(max(cur_w, req_w + 8), scr_w - 40)
            h = min(max(cur_h, req_h + 8), scr_h - 80)
            s = self._scale()
            if (w, h) != (cur_w, cur_h):
                self.geometry(f"{int(w / s)}x{int(h / s)}")
            self.minsize(int(min(req_w, scr_w - 40) / s), int(min(req_h * 0.8, scr_h - 80) / s))
        except tk.TclError:
            pass

    def _style_tree(self):
        s = self._scale()
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("D2M.Treeview", background="#242424", fieldbackground="#242424", foreground="#dce4ee",
                        rowheight=int(26 * s), borderwidth=0, font=("Segoe UI", 10))
        style.map("D2M.Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
        style.configure("D2M.Treeview.Heading", background="#1b1b1b", foreground="#dce4ee", relief="flat",
                        font=("Segoe UI", 10, "bold"), padding=(6, int(4 * s)))
        style.map("D2M.Treeview.Heading", background=[("active", "#2f2f2f")])
        style.layout("D2M.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def _apply_log_visibility(self):
        if self.settings.show_log:
            self.grid_rowconfigure(7, weight=1)
            self.log_box.grid(row=7, column=0, sticky="nsew", padx=14, pady=(0, 10))
            self.log_toggle.configure(text="Hide log ▾")
        else:
            self.grid_rowconfigure(7, weight=0)
            self.log_box.grid_remove()
            self.log_toggle.configure(text="Show log ▸")

    def _toggle_log(self):
        self.settings.show_log = not self.settings.show_log
        self._apply_log_visibility()
        self.settings.save()

    # ================================================================ helpers
    def _set_status(self, text: str, level: Optional[str] = None):
        self.status_label.configure(text=f"Status: {text}", text_color=LEVEL_COLORS.get(level or "info"))

    def _blog(self, text: str, level: str = "info", job_id: Optional[str] = None):
        entry = (f"{time.strftime('%H:%M:%S')} {text}", level)
        self._batch_log.append(entry)
        if job_id:
            self._job_logs.setdefault(job_id, collections.deque(maxlen=JOB_LOG_LINES)).append(entry)
        if self._log_view_job is None or (job_id and self._log_view_job == job_id) or self._log_view_job == "__none__":
            self._append_log_box(entry)

    def _append_log_box(self, entry: tuple):
        text, level = entry
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n", level if level in LEVEL_COLORS else None)
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > BATCH_LOG_LINES + 500:
            self.log_box.delete("1.0", f"{lines - BATCH_LOG_LINES}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _show_log_for(self, job_id: Optional[str]):
        if job_id == self._log_view_job:
            return
        self._log_view_job = job_id
        entries = self._batch_log if job_id is None else self._job_logs.get(job_id, [])
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        job = self.queue.get(job_id) if job_id else None
        if job is not None:
            self.log_box.insert("end", f"{job.name}\n{job.source}\n", "stage")
            if job.error:
                self.log_box.insert("end", f"{job.error_title or 'Error'}: {job.error}\n", "error")
            for w in job.warnings:
                self.log_box.insert("end", f"Warning: {w}\n", "warning")
            if job.output_dir:
                self.log_box.insert("end", f"Output: {job.output_dir}\n", "success")
            if not entries and job.log_path and Path(job.log_path).is_file():
                self.log_box.insert("end", f"(Detailed log: {job.log_path})\n", "debug")
        for text, level in entries:
            self.log_box.insert("end", text + "\n", level if level in LEVEL_COLORS else None)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _selected_job(self) -> Optional[Job]:
        ids = list(self.tree.selection())
        return self.queue.get(ids[0]) if ids else None

    def _save_settings(self):
        st = self.settings
        st.include_subfolders = bool(self.subdirs_var.get())
        st.output_beside_source = self.beside_var.get() == "beside"
        st.last_output_dir = self.out_entry.get().strip().strip('"')
        st.ocr_enabled = bool(self.ocr_var.get())
        st.normalize()
        st.save()

    def _on_output_mode(self, save: bool = True):
        running = bool(self._runner and self._runner.is_alive())
        state = "normal" if self.beside_var.get() == "root" and not running else "disabled"
        self.out_entry.configure(state=state)
        self.out_btn.configure(state=state)
        if save:
            self._save_settings()

    def _enable_dnd(self):
        self._dnd = False
        if _DND_AVAILABLE:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._on_drop)
                self._dnd = True
                self.empty_label.configure(text="Drag & drop PDFs or folders here, or use Add PDFs / Add Folder.")
            except Exception:
                self._dnd = False

    # ================================================================ queue view
    def _row_values(self, job: Job) -> tuple:
        elapsed = job.elapsed_s
        status = job.status_label
        if job.status == RUNNING:
            start = _parse_iso(job.started_at)
            elapsed = time.time() - start if start else None
            status = f"Running — {job.stage}" if job.stage else "Running"
        return job.name, status, _fmt_secs(elapsed)

    def _refresh_tree(self):
        existing = set(self.tree.get_children())
        wanted = [j.id for j in self.queue.jobs]
        for iid in existing - set(wanted):
            self.tree.delete(iid)
        for index, job in enumerate(self.queue.jobs):
            if self.tree.exists(job.id):
                self.tree.item(job.id, values=self._row_values(job), tags=(job.status,))
                if self.tree.index(job.id) != index:
                    self.tree.move(job.id, "", index)
            else:
                self.tree.insert("", index, iid=job.id, values=self._row_values(job), tags=(job.status,))
        if self.queue.jobs:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self._update_overall()

    def _update_row(self, job: Job):
        if self.tree.exists(job.id):
            self.tree.item(job.id, values=self._row_values(job), tags=(job.status,))

    def _update_overall(self):
        jobs = self.queue.jobs
        done = sum(1 for j in jobs if j.status in (COMPLETED, WARNINGS))
        failed = sum(1 for j in jobs if j.status in (FAILED, PARTIAL))
        text = f"{done} of {len(jobs)} files completed" if jobs else ""
        if failed:
            text += f" · {failed} failed"
        self.overall_label.configure(text=text)

    def _on_select(self):
        job = self._selected_job()
        self._show_log_for(job.id if job else None)
        self._update_controls()

    # ================================================================ adding
    def _browse_pdfs(self):
        paths = filedialog.askopenfilenames(
            parent=self, title="Add PDF datasheets", initialdir=self.settings.last_input_dir or str(Path.home()),
            filetypes=[("PDF datasheets", "*.pdf"), ("All files", "*.*")])
        if paths:
            self.settings.last_input_dir = str(Path(paths[0]).parent)
            self._add_paths(list(paths))

    def _browse_folder(self):
        path = filedialog.askdirectory(parent=self, title="Add all PDFs in a folder",
                                       initialdir=self.settings.last_input_dir or str(Path.home()))
        if path:
            self.settings.last_input_dir = str(Path(path))
            self._add_paths([path])

    def _on_drop(self, event):
        try:
            items = self.tk.splitlist(event.data)
        except tk.TclError:
            items = [event.data]
        self._add_paths(list(items))

    def _add_paths(self, paths: List[str]):
        res = self.queue.add_paths(paths, recursive=bool(self.subdirs_var.get()))
        self.queue.save()
        self._save_settings()
        self._refresh_tree()
        level = "warning" if (res.duplicates or res.not_pdf or res.missing) else "info"
        self._blog(res.message(), level)
        for item in (res.not_pdf + res.missing + res.duplicates)[:10]:
            self._blog(f"  skipped: {item}", "debug")
        self._set_status(res.message(), level if not res.added else None)
        self._update_controls()

    def _remove_selected(self):
        ids = list(self.tree.selection())
        running = self._runner.current_job_id if self._runner and self._runner.is_alive() else None
        n = self.queue.remove([i for i in ids if i != running])
        self.queue.save()
        self._refresh_tree()
        self._set_status(f"Removed {n} file(s) from the list (output folders are not touched).")
        self._on_select()

    def _clear(self):
        n = self.queue.clear()
        self.queue.save()
        self._refresh_tree()
        self._set_status(f"Cleared {n} file(s) from the list (output folders are not touched).")
        self._on_select()

    # ================================================================ batch control
    def _output_root(self) -> tuple:
        """(ok, root|None)"""
        if self.beside_var.get() == "beside":
            return True, None
        text = self.out_entry.get().strip().strip('"')
        if not text:
            messagebox.showwarning(APP_NAME, "Choose an output folder, or select 'Beside each source PDF'.", parent=self)
            return False, None
        root = Path(text)
        if root.exists() and not root.is_dir():
            messagebox.showerror(APP_NAME, f"The output location is not a folder:\n\n{root}", parent=self)
            return False, None
        return True, root

    def _convert(self):
        if self._runner and self._runner.is_alive():
            return
        if not (self._docling and self._docling.available):
            messagebox.showerror("Docling not available", DOCLING_NOT_FOUND_MESSAGE, parent=self)
            return
        if not self.queue.next_runnable():
            self._set_status("Nothing to convert: add PDFs (completed files are not converted again).", "warning")
            return
        ok, root = self._output_root()
        if not ok:
            return
        self._save_settings()
        from app import batch_options_from

        options, device_note = batch_options_from(self.settings, self._docling, output_root=root,
                                                  ocr=bool(self.ocr_var.get()))
        self._runner = BatchRunner(self.queue, self._docling, options,
                                   lambda kind, payload: self._events.put((kind, payload)))
        self._batch_started = time.monotonic()
        self._blog(f"=== Convert: output {root or 'beside each PDF'}; OCR {'ON' if options.ocr else 'off'}; "
                   f"{device_note}", "stage")
        self._runner.start()
        self.stage_bar.grid()
        self.stage_bar.start()
        self._update_controls()

    def _cancel_current(self):
        r = self._runner
        if r and r.is_alive() and r.current_job_id:
            job = self.queue.get(r.current_job_id)
            self._blog(f"Cancelling {job.name if job else 'current file'}…", "warning")
            r.cancel_current()
            self.cancel_btn.configure(state="disabled")

    def _stop_batch(self):
        r = self._runner
        if r and r.is_alive():
            self._blog("Stopping: the current file is cancelled and no further file is started.", "warning")
            r.stop()
            self._update_controls()

    def _update_controls(self):
        running = bool(self._runner and self._runner.is_alive())
        ready = bool(self._docling and self._docling.available)
        stopping = running and self._runner.state == "stopping"
        has_work = self.queue.next_runnable() is not None
        self.convert_btn.configure(state="normal" if ready and not running and has_work else "disabled")
        current = self._runner.current_job_id if running else None
        self.cancel_btn.configure(state="normal" if current and not stopping else "disabled")
        self.stop_btn.configure(state="normal" if running and not stopping else "disabled")
        for w in (self.beside_rb, self.root_rb, self.ocr_cb, self.clear_btn):
            w.configure(state="disabled" if running else "normal")
        self._on_output_mode(save=False)
        job = self._selected_job()
        self.open_btn.configure(state="normal" if job and job.output_dir and Path(job.output_dir).is_dir() else "disabled")
        self.open_md_btn.configure(state="normal" if job and job.markdown_path and Path(job.markdown_path).is_file()
                                   else "disabled")
        sel = list(self.tree.selection())
        self.remove_btn.configure(state="normal" if sel and sel != [current] else "disabled")
        if not running and not self._detecting:
            if not ready:
                self._set_status("Docling is not available. Open Settings… for diagnostics, then retry.", "error")
            elif not self.queue.jobs:
                self._set_status("Ready. Add PDFs to start.")

    # ================================================================ docling detection
    def _start_detection(self):
        if self._detecting:
            return
        self._detecting = True
        self.docling_label.configure(text="Checking Docling installation…", text_color="gray70")
        self._set_status("Checking Docling installation…")
        self._update_controls()
        explicit, backend = self.settings.docling_path, self.settings.engine_backend

        def work():
            try:
                info = detect_docling(explicit, timeout=120, backend=backend)
            except Exception as exc:  # never crash on detection
                info = DoclingInfo(False, error=f"{exc.__class__.__name__}: {exc}")
            try:
                from core.gpu import gpu_status

                gst = gpu_status(info)
            except Exception:
                gst = None
            self._events.put(("docling", (info, gst)))

        threading.Thread(target=work, daemon=True, name="detect-docling").start()

    def _on_docling_detected(self, info: DoclingInfo, gst):
        self._detecting = False
        self._docling = info
        self._gpu_status = gst
        if info.available:
            from core.gpu import resolve_device

            device = resolve_device(self.settings.device, info, self.settings.gpu_verified, gst)[1].split(" - ")[0]
            models = "local models" if resolve_artifacts_path(self.settings.artifacts_path) else "default model cache"
            self.docling_label.configure(text=f"✓ Docling {info.version} · {models} · {device}",
                                         text_color=LEVEL_COLORS["success"])
            self._blog(f"Docling {info.version} found ({info.backend}, {info.seconds:.1f}s): {info.path}", "success")
            if info.error:
                self._blog(info.error, "warning")
            if info.version_note():
                self._blog(info.version_note(), "warning")
            n = sum(1 for j in self.queue.jobs if j.runnable)
            self._set_status(f"Ready: {n} file(s) to convert. Press Convert." if n else "Ready. Add PDFs to start.")
        else:
            self.docling_label.configure(text="✗ Docling not available — open Settings… for details",
                                         text_color=LEVEL_COLORS["error"])
            first = (info.error or "Docling was not found.").splitlines()[0]
            self._blog(f"Docling not available: {first}", "error")
        self._update_controls()
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            self._settings_dialog._refresh()

    # ================================================================ events
    def _poll_events(self):
        try:
            for _ in range(400):  # bounded work per tick keeps the UI responsive
                kind, payload = self._events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        if not self._closing:
            self.after(100, self._poll_events)

    def _handle_event(self, kind: str, payload):
        if kind == "job":
            job = self.queue.get(payload["id"])
            if job:
                self._update_row(job)
                if job.status not in (RUNNING, "pending"):
                    lvl = {COMPLETED: "success", WARNINGS: "warning", CANCELLED: "warning"}.get(job.status, "error")
                    self._blog(f"{job.status_label}: {job.name} ({_fmt_secs(job.elapsed_s)})"
                               + (f" → {job.output_dir}" if job.output_dir else "")
                               + (f" — {job.error.splitlines()[0]}" if job.error and job.status != CANCELLED else ""),
                               lvl, job.id)
                    sel = self._selected_job()
                    if sel and sel.id == job.id:
                        self._log_view_job = "__refresh__"
                        self._show_log_for(job.id)
                elif job.status == RUNNING and job.stage == "Preparing":
                    self._blog(f"--- {job.name}", "stage", job.id)
                    if self.tree.exists(job.id):
                        self.tree.see(job.id)
                self._update_overall()
                self._update_controls()
        elif kind == "log":
            job_id, ev = payload
            if ev.kind == "stage":
                self._blog(f"{ev.text}…", "stage", job_id)
            elif ev.level != "debug" or self.settings.debug_mode:
                self._blog("  " + ev.text, ev.level, job_id)
        elif kind == "state":
            self._update_controls()
        elif kind == "done":
            self._on_batch_done(payload)
        elif kind == "docling":
            self._on_docling_detected(*payload)
        elif kind == "gpu_test":
            ok, msg, sig = payload
            self.settings.gpu_verified = sig if ok else ""
            self.settings.save()
            self._blog(msg, "success" if ok else "warning")
            if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
                self._settings_dialog.gpu_test_done(ok, msg)

    def _on_batch_done(self, summary: dict):
        self.stage_bar.stop()
        self.stage_bar.grid_remove()
        s = summary
        text = (f"Finished: {s['succeeded']} converted, {s['failed']} failed, {s['cancelled']} cancelled "
                f"({_fmt_secs(s['duration_s'])}).")
        self._blog(text, "success" if not s["failed"] and not s["cancelled"] else "warning")
        self._set_status(text, "success" if not s["failed"] and not s["cancelled"] else "warning")
        self._update_controls()
        self._update_overall()
        try:
            self.bell()
        except tk.TclError:
            pass

    def _tick(self):
        r = self._runner
        if r and r.is_alive():
            job = self.queue.get(r.current_job_id) if r.current_job_id else None
            if job:
                self._update_row(job)
                start = _parse_iso(job.started_at)
                el = _fmt_secs(time.time() - start) if start else ""
                extra = "  (stopping…)" if r.state == "stopping" else ""
                self._set_status(f"{job.name} — {job.stage or 'working'} ({el}){extra}")
        if not self._closing:
            self.after(1000, self._tick)

    # ================================================================ open / copy / settings
    def _open(self, path: Optional[str], what: str):
        if path and Path(path).exists():
            try:
                os.startfile(path)  # type: ignore[attr-defined]
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"Could not open the {what}:\n\n{exc}", parent=self)
        else:
            self._set_status(f"The {what} is not available for the selected file.", "warning")

    def _open_output(self):
        job = self._selected_job()
        self._open(job.output_dir if job else None, "output folder")

    def _open_markdown(self):
        job = self._selected_job()
        self._open(job.markdown_path if job else None, "Markdown file")

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_box.get("1.0", "end"))
        self._set_status("Log copied to clipboard.")

    def _open_settings(self):
        if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
            self._settings_dialog.lift()
            return
        self._settings_dialog = SettingsDialog(self)

    def _browse_output(self):
        initial = self.out_entry.get().strip() or self.settings.last_output_dir or self.settings.last_input_dir
        path = filedialog.askdirectory(parent=self, title="Select the output folder",
                                       initialdir=initial or str(Path.home()))
        if path:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, str(Path(path)))
            self._save_settings()

    def _on_close(self):
        if self._runner and self._runner.is_alive():
            if not messagebox.askyesno(APP_NAME, "A conversion is running. Stop it and exit?", parent=self):
                return
            self._runner.stop()
            self._runner.join(timeout=20)
        try:
            self.settings.window_geometry = self.geometry()
            self._save_settings()
        except tk.TclError:
            pass
        self.queue.save()
        self._closing = True
        self.destroy()

    def report_callback_exception(self, exc, val, tb):  # Tk callback errors -> dialog, not crash
        import traceback

        detail = "".join(traceback.format_exception(exc, val, tb))
        try:
            logs_dir().mkdir(parents=True, exist_ok=True)
            with (logs_dir() / "gui_errors.log").open("a", encoding="utf-8") as fh:
                fh.write(detail + "\n")
        except OSError:
            pass
        TextDialog(self, "Unexpected error", detail)
