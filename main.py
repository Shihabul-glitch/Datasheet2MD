"""Datasheet2MD entry point.

    Datasheet2MD.exe                          -> GUI
    Datasheet2MD.exe a.pdf b.pdf folder\      -> GUI with these PDFs in the list (not started)
    Datasheet2MD.exe --cli INPUT [INPUT ...] [options]   -> convert without a window
    Datasheet2MD.exe --self-test              -> check the installation (exit 0 = ready)

Command-line exit codes:
    0   every file converted (warnings included)
    1   every file failed (or was incomplete)
    2   cancelled (Ctrl+C)
    3   Docling not found / engine unavailable
    4   some files succeeded, some failed
    5   no PDF files found in the inputs
    64  invalid or removed command-line option
"""

from __future__ import annotations

import argparse
import sys

EXIT_OK, EXIT_ALL_FAILED, EXIT_CANCELLED, EXIT_NO_ENGINE, EXIT_SOME_FAILED, EXIT_NO_INPUT, EXIT_USAGE = (
    0, 1, 2, 3, 4, 5, 64)

# Options of Datasheet2MD 2.0 features that were removed in 2.1 (rejected, never silently ignored).
REMOVED_OPTIONS = {
    "--overwrite": "existing output is never overwritten any more; a new folder (NAME_2, ...) is created",
    "--collision": "existing output is never overwritten or skipped any more; a new folder (NAME_2, ...) is created",
    "--report-csv": "CSV reports were removed; use --report FILE.json",
    "--timeout": "per-file time limits were removed; press Ctrl+C to cancel",
    "--retries": "automatic retries were removed (each PDF is converted exactly once)",
    "--continue-on-error": "continuing after a failed file is the default behaviour",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would exit with 2, which means "cancelled" here
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="Datasheet2MD", description="Electronic datasheet PDF -> Markdown + PNG images (local Docling)")
    p.add_argument("inputs", nargs="*", metavar="INPUT", help="PDF files and/or folders")
    p.add_argument("--cli", action="store_true", help="convert without opening a window")
    p.add_argument("--self-test", action="store_true", help="check Docling detection and exit")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--out", metavar="DIR", help="output folder (one sub-folder per PDF)")
    out.add_argument("--beside", action="store_true", help="write each result next to its source PDF (default)")
    p.add_argument("--recursive", "-r", action="store_true", help="include PDFs in sub-folders of input folders")
    p.add_argument("--ocr", action="store_true", help="scanned PDF: enable OCR (slower)")
    p.add_argument("--ocr-mode", choices=["off", "on", "auto", "force"],
                   help="v2.0 option: 'on' = --ocr, 'off' = default; 'auto'/'force' were removed")
    p.add_argument("--device", choices=["auto", "cpu", "gpu"], help="processing device (default: setting, auto)")
    p.add_argument("--no-cleanup", action="store_true", help="disable LLM-friendly cleanup")
    p.add_argument("--debug", action="store_true", help="verbose log, keep intermediate files")
    p.add_argument("--pages", help="page range, e.g. 1-4 (applies to every file)")
    p.add_argument("--report", metavar="FILE.json", help="write a JSON batch summary")
    p.add_argument("--stop-on-error", action="store_true", help="stop the batch after the first failed file")
    p.add_argument("--quiet", "-q", action="store_true", help="only print per-file results and the summary")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for arg in argv:
        name = arg.split("=", 1)[0]
        if name in REMOVED_OPTIONS:
            sys.stderr.write(f"Datasheet2MD: option {name} was removed in 2.1: {REMOVED_OPTIONS[name]}.\n")
            return EXIT_USAGE
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.ocr_mode in ("auto", "force"):
        parser.error(f"--ocr-mode {args.ocr_mode} was removed in 2.1 (use --ocr for scanned PDFs)")
    if args.ocr_mode == "on":
        args.ocr = True

    if args.self_test:
        from app import run_self_test

        return run_self_test()
    if args.cli:
        if not args.inputs:
            parser.error("--cli requires at least one PDF file or folder")
        if args.pages:
            import re

            if not re.fullmatch(r"\s*\d+\s*(-\s*\d+\s*)?", args.pages):
                parser.error("--pages must look like 5 or 1-4")
        from app import run_cli

        return run_cli(args)
    from app import run_gui

    return run_gui(args.inputs)


if __name__ == "__main__":
    sys.exit(main())
