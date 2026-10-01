"""Structural checks of a converted document folder.

These checks confirm that the output is complete and well-formed (Markdown
present and valid UTF-8, image links relative, inside the folder and
resolving to real PNG files, no base64 images, cleanup kept tables/headings,
source PDF unchanged). They do NOT prove that every technical value was
extracted correctly.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .image_manager import clean_target, is_data_uri, is_png, is_remote, iter_image_refs

OK, WARN, FAIL = "ok", "warn", "fail"
SYMBOL = {OK: "✓", WARN: "⚠", FAIL: "✗"}

_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_IMAGE_NAME_RE = re.compile(r"^image_(\d+)\.[A-Za-z0-9]+$")
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
LITTLE_TEXT_CHARS_PER_PAGE = 150


@dataclass
class Check:
    status: str
    message: str

    def line(self) -> str:
        return f"{SYMBOL[self.status]} {self.message}"


@dataclass
class ValidationReport:
    checks: List[Check] = field(default_factory=list)
    image_count: int = 0
    table_count: int = 0
    heading_count: int = 0
    text_chars: int = 0

    def add(self, status: str, message: str) -> None:
        self.checks.append(Check(status, message))

    @property
    def passed(self) -> bool:
        return all(c.status != FAIL for c in self.checks)

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    def lines(self) -> List[str]:
        return [c.line() for c in self.checks]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _outside_fences(md: str) -> List[str]:
    lines, fence = [], None
    for line in md.split("\n"):
        m = re.match(r"^\s{0,3}(```|~~~)", line)
        if fence is not None:
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        lines.append(line)
    return lines


def count_tables(md: str) -> int:
    lines = _outside_fences(md)
    count = 0
    for i in range(1, len(lines)):
        header_starts_block = i < 2 or not lines[i - 2].lstrip().startswith("|")
        if (_TABLE_SEPARATOR_RE.match(lines[i]) and "-" in lines[i]
                and lines[i - 1].lstrip().startswith("|") and header_starts_block):
            count += 1
    return count


def count_headings(md: str) -> int:
    return sum(1 for line in _outside_fences(md) if _HEADING_RE.match(line))


def text_char_count(md: str) -> int:
    """Non-whitespace characters of real text (image links and table rules removed)."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.DOTALL)
    t = re.sub(r"[|\-:#*_`>]", "", t)
    return sum(1 for c in t if not c.isspace())


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_output(
    markdown_path: Path,
    images_dir: Path,
    source_pdf: Optional[Path] = None,
    source_hash_before: Optional[str] = None,
    expected_tables: Optional[int] = None,
    expected_headings: Optional[int] = None,
) -> ValidationReport:
    report = ValidationReport()
    folder = markdown_path.parent

    if not markdown_path.is_file() or markdown_path.stat().st_size == 0:
        report.add(FAIL, "Markdown file was not created or is empty")
        return report
    report.add(OK, "Markdown created")

    raw = markdown_path.read_bytes()
    try:
        md = raw.decode("utf-8")
        report.add(OK, "UTF-8 validation passed")
    except UnicodeDecodeError as exc:
        report.add(FAIL, f"Markdown is not valid UTF-8 ({exc})")
        md = raw.decode("utf-8", errors="replace")
    report.text_chars = text_char_count(md)

    targets = [clean_target(r.target) for r in iter_image_refs(md)
               if not is_remote(r.target) and not is_data_uri(r.target)]
    files = sorted((p for p in images_dir.iterdir() if p.is_file()) if images_dir.is_dir() else [],
                   key=lambda p: p.name)
    report.image_count = len(files)
    if files:
        report.add(OK, f"{len(files)} image{'s' if len(files) != 1 else ''} extracted")
    elif targets:
        report.add(FAIL, "Markdown references images but no images/ folder was created")

    n_data = len(re.findall(r"data:image/", md, re.IGNORECASE))
    if n_data:
        report.add(FAIL, f"{n_data} embedded base64 image(s) remain in the Markdown")

    problems = []
    for t in targets:
        if t.lower().startswith("file:") or _ABSOLUTE_RE.match(t):
            problems.append(f"absolute path: {t}")
        elif "\\" in t:
            problems.append(f"backslash in path: {t}")
        else:
            p = folder / t
            if not _inside(p, folder):
                problems.append(f"outside the document folder: {t}")
            elif not p.is_file():
                problems.append(f"missing file: {t}")
            elif p.stat().st_size == 0:
                problems.append(f"empty file: {t}")
            elif p.suffix.lower() == ".png" and not is_png(p):
                problems.append(f"not a valid PNG: {t}")
    if problems:
        shown = "; ".join(problems[:5]) + (" ..." if len(problems) > 5 else "")
        report.add(FAIL, f"{len(problems)} invalid image link(s): {shown}")
    elif targets:
        report.add(OK, "All image links valid (relative, inside the document folder)")
    missing = md.count("*[Image missing:")
    if missing:
        report.add(WARN, f"{missing} image(s) referenced by Docling were missing and replaced by a note")

    if files:
        numbers = [int(m.group(1)) if (m := _IMAGE_NAME_RE.match(f.name)) else None for f in files]
        if None in numbers or sorted(numbers) != list(range(1, len(files) + 1)):
            report.add(FAIL, "Image files are not numbered image_001, image_002, ... without gaps")

    report.table_count = count_tables(md)
    report.heading_count = count_headings(md)
    if expected_tables is not None and report.table_count < expected_tables:
        report.add(FAIL, f"Tables lost during cleanup ({report.table_count} of {expected_tables})")
    else:
        report.add(OK, f"{report.table_count} Markdown table{'s' if report.table_count != 1 else ''}")
    if expected_headings is not None and report.heading_count < expected_headings:
        report.add(FAIL, f"Headings lost during cleanup ({report.heading_count} of {expected_headings})")

    glyphs = len(re.findall(r"GLYPH<[^>]*>|/uni[0-9A-Fa-f]{4}\b", md))
    if glyphs:
        report.add(WARN, f"{glyphs} unreadable font glyph(s) in the text (PDF font encoding issue)")

    if source_pdf is not None and source_hash_before:
        try:
            unchanged = sha256_file(source_pdf) == source_hash_before
        except OSError:
            unchanged = False
        report.add(OK if unchanged else FAIL,
                   "Source PDF unchanged" if unchanged else "Source PDF was modified or is unreadable!")
    return report


def little_text_warning(text_chars: int, pages: int, ocr_enabled: bool) -> Optional[str]:
    """A hint (not a failure) when very little text came out of a non-OCR conversion."""
    if ocr_enabled or pages <= 0:
        return None
    per_page = text_chars / pages
    if per_page >= LITTLE_TEXT_CHARS_PER_PAGE:
        return None
    return (f"Little text was extracted (about {per_page:.0f} characters per page). If this is a scanned or "
            "image-based PDF, tick 'Scanned PDF / Enable OCR' and convert it again. (Diagram-only or "
            "blank pages also produce little text.)")
