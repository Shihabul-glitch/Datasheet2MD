"""Post-conversion validation of the generated Markdown folder."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .image_manager import is_png, local_image_targets

OK, WARN, FAIL = "ok", "warn", "fail"
SYMBOL = {OK: "✓", WARN: "⚠", FAIL: "✗"}

_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_IMAGE_NAME_RE = re.compile(r"^image_(\d+)\.[A-Za-z0-9]+$")
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


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


def validate_output(
    markdown_path: Path,
    images_dir: Path,
    source_pdf: Optional[Path] = None,
    source_hash_before: Optional[str] = None,
    expected_tables: Optional[int] = None,
    expected_headings: Optional[int] = None,
) -> ValidationReport:
    report = ValidationReport()

    # 1. Markdown exists and is not empty
    if not markdown_path.is_file() or markdown_path.stat().st_size == 0:
        report.add(FAIL, "Markdown file was not created")
        return report
    report.add(OK, "Markdown created")

    # 2. UTF-8
    raw = markdown_path.read_bytes()
    try:
        md = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        report.add(FAIL, f"Markdown is not valid UTF-8 ({exc})")
        md = raw.decode("utf-8", errors="replace")
    else:
        if raw.startswith(b"\xef\xbb\xbf"):
            report.add(WARN, "UTF-8 validation passed (file has a BOM)")
        else:
            report.add(OK, "UTF-8 validation passed")

    # 3. Images: extraction count, references, numbering
    targets = local_image_targets(md)
    files = sorted(
        (p for p in images_dir.iterdir() if p.is_file()) if images_dir.is_dir() else [],
        key=lambda p: p.name,
    )
    report.image_count = len(files)
    if files:
        report.add(OK, f"{len(files)} image{'s' if len(files) != 1 else ''} extracted")
    elif targets:
        report.add(FAIL, "Markdown references images but no images/ folder was created")
    else:
        report.add(OK, "No figures found in the document (no images to extract)")

    # 4. No base64 images
    n_data = len(re.findall(r"data:image/", md, re.IGNORECASE))
    if n_data:
        report.add(FAIL, f"{n_data} embedded base64 image(s) remain in the Markdown")
    else:
        report.add(OK, "No embedded base64 images")

    # 5. Every reference is relative, forward-slash, and resolves to a real file
    problems = []
    for t in targets:
        if t.lower().startswith("file:") or _ABSOLUTE_RE.match(t):
            problems.append(f"absolute path: {t}")
        elif "\\" in t:
            problems.append(f"backslash in path: {t}")
        else:
            p = markdown_path.parent / t
            if not p.is_file():
                problems.append(f"missing file: {t}")
            elif p.stat().st_size == 0:
                problems.append(f"empty file: {t}")
            elif p.suffix.lower() == ".png" and not is_png(p):
                problems.append(f"not a valid PNG: {t}")
    missing_placeholders = md.count("*[Image missing:")
    if problems:
        shown = "; ".join(problems[:5]) + (" ..." if len(problems) > 5 else "")
        report.add(FAIL, f"{len(problems)} invalid image reference(s): {shown}")
    elif targets:
        report.add(OK, "All image references valid")
    if missing_placeholders:
        report.add(WARN, f"{missing_placeholders} image(s) referenced by Docling were missing and replaced by a note")

    # 6. Sequential numbering in order of appearance
    numbers = []
    for f in files:
        m = _IMAGE_NAME_RE.match(f.name)
        numbers.append(int(m.group(1)) if m else None)
    if files:
        if None in numbers or sorted(numbers) != list(range(1, len(files) + 1)):
            report.add(FAIL, "Image files are not numbered image_001, image_002, ... without gaps")
        else:
            first_seen = []
            for t in targets:
                m = _IMAGE_NAME_RE.match(Path(t).name)
                if m and int(m.group(1)) not in first_seen:
                    first_seen.append(int(m.group(1)))
            if first_seen != sorted(first_seen):
                report.add(FAIL, "Image numbering does not follow document order")
            else:
                report.add(OK, "Image numbering sequential")
        referenced = {Path(t).name.lower() for t in targets}
        orphans = [f.name for f in files if f.name.lower() not in referenced]
        if orphans:
            report.add(WARN, f"{len(orphans)} image file(s) not referenced by the Markdown")

    # 7. Tables and headings preserved through cleanup
    report.table_count = count_tables(md)
    report.heading_count = count_headings(md)
    if expected_tables is not None and report.table_count < expected_tables:
        report.add(FAIL, f"Tables lost during cleanup ({report.table_count} of {expected_tables})")
    else:
        report.add(OK, f"{report.table_count} Markdown table{'s' if report.table_count != 1 else ''} preserved")
    if expected_headings is not None and report.heading_count < expected_headings:
        report.add(FAIL, f"Headings lost during cleanup ({report.heading_count} of {expected_headings})")

    # 8. Extraction artifacts worth knowing about
    glyphs = len(re.findall(r"GLYPH<[^>]*>|/uni[0-9A-Fa-f]{4}\b", md))
    if glyphs:
        report.add(WARN, f"{glyphs} unreadable font glyph(s) in the text (PDF font encoding issue)")
    if "�" in md:
        report.add(WARN, f"{md.count(chr(0xFFFD))} unknown character(s) (�) in the text")

    # 9. Source PDF untouched
    if source_pdf is not None and source_hash_before:
        try:
            unchanged = sha256_file(source_pdf) == source_hash_before
        except OSError:
            unchanged = False
        report.add(OK if unchanged else FAIL,
                   "Source PDF unchanged" if unchanged else "Source PDF was modified or is unreadable!")
    return report
