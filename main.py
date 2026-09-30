"""Datasheet2MD entry point.

    Datasheet2MD.exe                      -> GUI
    Datasheet2MD.exe some.pdf             -> GUI with the PDF preselected
    python main.py --cli some.pdf [--out DIR] [--ocr] [--no-cleanup] [--debug] [--overwrite] [--pages 1-4]
"""

from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="Datasheet2MD", description="Electronic datasheet PDF -> Markdown (Docling)")
    parser.add_argument("pdf", nargs="?", help="PDF datasheet to convert or preselect")
    parser.add_argument("--cli", action="store_true", help="convert without opening a window")
    parser.add_argument("--out", help="output root folder (default: next to the PDF)")
    parser.add_argument("--ocr", action="store_true", help="scanned PDF: enable OCR")
    parser.add_argument("--no-cleanup", action="store_true", help="disable LLM-friendly cleanup")
    parser.add_argument("--debug", action="store_true", help="verbose log, keep intermediate files")
    parser.add_argument("--overwrite", action="store_true", help="overwrite generated files instead of creating a new version")
    parser.add_argument("--pages", help="page range for testing, e.g. 1-4")
    args = parser.parse_args(argv)

    from app import run_cli, run_gui

    if args.cli:
        if not args.pdf:
            parser.error("--cli requires a PDF path")
        return run_cli(args.pdf, args.out, args.ocr, not args.no_cleanup, args.debug, args.overwrite, args.pages)
    return run_gui(args.pdf)


if __name__ == "__main__":
    sys.exit(main())
