"""Deterministic, conservative Markdown cleanup for datasheet conversions.

Rules of this module:

* Never rewrite, summarize or reinterpret technical prose.
* Never touch numbers, units (°C, µA, kΩ, ±, ...), hex/binary values or equations.
* Never touch fenced code blocks; table rows are only touched by the narrowly
  scoped I²C fix and trailing-whitespace trimming.
* Only remove a line when it is unambiguously a page artifact.

Mandatory step (always run): ``extract_embedded_images`` - base64 images are
decoded into files and replaced by relative references.

"LLM-friendly cleanup" steps (optional): I²C notation, page-number/footer
removal, figure captions as alt text, whitespace and blank-line cleanup.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Tuple
from urllib.parse import unquote_to_bytes

from .image_manager import HTML_IMG_RE, MD_IMAGE_RE, fenced_code_spans

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")

_DATA_URI_RE = re.compile(
    r"^\s*<?\s*data:(?P<mime>image/[\w.+-]+)?(?P<params>(?:;[\w.+-]+=[^;,]*)*)"
    r"(?P<b64>;base64)?,(?P<data>.*?)\s*>?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/svg+xml": ".svg", "image/tiff": ".tif",
}

# "I 2 C", "I 2C", "I2 C" (superscript lost during extraction) -> I²C.
# Plain "I2C" is left alone (it is correct and often part of identifiers).
_I2C_RE = re.compile(r"(?<![\w/])I(?: 2 ?C| ?2 C)(?!\w)")

_PAGE_NUMBER_RE = re.compile(
    r"^(?:page\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?|\d{1,4}\s*(?:of|/)\s*\d{1,4}|[-–—]\s*\d{1,4}\s*[-–—])$",
    re.IGNORECASE,
)
_FOOTER_KEYWORD_RE = re.compile(
    r"(©|\(c\)\s*\d{4}|copyright|all rights reserved|confidential|proprietary|"
    r"www\.|https?://|document\s*(?:no\.?|number|#)|doc\.?\s*#|\brev(?:ision)?\.?\s*[\dA-Z][\w.]*|"
    r"data\s?sheet)",
    re.IGNORECASE,
)
_CAPTION_RE = re.compile(
    r"^(?:\*\*|__|\*|_)?(?:Figure|Fig\.?|Diagram)\s*[A-Z]?\d+(?:[-.–]\d+)*[a-z]?\s*[:.\-–—]\s*\S.*$",
    re.IGNORECASE,
)
_STANDALONE_IMAGE_RE = re.compile(r"^\s*!\[(?P<alt>[^\]]*)\]\((?P<rest>[^)]*)\)\s*$")


@dataclass
class CleanReport:
    base64_extracted: int = 0
    i2c_fixed: int = 0
    page_artifacts_removed: int = 0
    captions_added: int = 0
    blank_lines_removed: int = 0
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> List[str]:
        items = []
        if self.base64_extracted:
            items.append(f"{self.base64_extracted} embedded base64 image(s) extracted")
        if self.i2c_fixed:
            items.append(f"{self.i2c_fixed} I²C notation fix(es)")
        if self.page_artifacts_removed:
            items.append(f"{self.page_artifacts_removed} page number/footer line(s) removed")
        if self.captions_added:
            items.append(f"{self.captions_added} figure caption(s) used as image alt text")
        if self.blank_lines_removed:
            items.append(f"{self.blank_lines_removed} redundant blank line(s) removed")
        return items


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _map_prose_lines(md: str, fn: Callable[[str, bool], str]) -> str:
    """Apply ``fn(line, is_table_row)`` to every line outside fenced code blocks."""
    out, fence = [], None
    for line in md.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is not None:
            out.append(line)
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        out.append(fn(line, line.lstrip().startswith("|")))
    return "\n".join(out)


def _is_plain_paragraph(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("|", "#", "!", "<", ">", "-", "*", "+", "`", "~")) and not re.match(r"^\d+[.)]\s", s)


def _decode_data_uri(uri: str) -> Tuple[bytes, str]:
    m = _DATA_URI_RE.match(uri)
    if not m:
        raise ValueError("not a data URI")
    mime = (m.group("mime") or "image/png").lower()
    data = m.group("data")
    if m.group("b64"):
        compact = re.sub(r"\s+", "", data)
        compact += "=" * (-len(compact) % 4)
        raw = base64.b64decode(compact, validate=False)
    else:
        raw = unquote_to_bytes(data)
    if not raw:
        raise ValueError("empty image data")
    return raw, _MIME_EXT.get(mime, ".png")


# --------------------------------------------------------------------------- #
# mandatory step
# --------------------------------------------------------------------------- #
def extract_embedded_images(md: str, dest_dir: Path, rel_dir: str) -> Tuple[str, int, List[str]]:
    """Decode every ``data:image/...`` image into ``dest_dir`` and relink it.

    ``rel_dir`` is ``dest_dir`` relative to the Markdown's base directory (so
    image_manager can pick the files up in document order afterwards).
    Returns (markdown, extracted_count, warnings).
    """
    if "data:" not in md.lower():
        return md, 0, []
    spans = fenced_code_spans(md)
    warnings: List[str] = []
    count = 0

    def save(uri: str) -> str:
        nonlocal count
        raw, ext = _decode_data_uri(uri)
        count += 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = f"embedded_{count:04d}{ext}"
        (dest_dir / name).write_bytes(raw)
        return f"{rel_dir}/{name}" if rel_dir else name

    def md_repl(m: "re.Match[str]") -> str:
        if any(a <= m.start() < b for a, b in spans):
            return m.group(0)
        target = m.group("target")
        if not target.strip("<> ").lower().startswith("data:"):
            return m.group(0)
        try:
            return f"![{m.group('alt')}]({save(target.strip('<> '))})"
        except (ValueError, binascii.Error, OSError) as exc:
            warnings.append(f"An embedded image could not be decoded and was removed ({exc}).")
            return "*[Embedded image could not be decoded]*"

    def html_repl(m: "re.Match[str]") -> str:
        if any(a <= m.start() < b for a, b in spans):
            return m.group(0)
        target = m.group("target")
        if not target.strip().lower().startswith("data:"):
            return m.group(0)
        alt_m = re.search(r"\balt\s*=\s*([\"'])(.*?)\1", m.group(0), re.IGNORECASE | re.DOTALL)
        alt = (alt_m.group(2) if alt_m else "Image").replace("[", "(").replace("]", ")")
        try:
            return f"![{alt}]({save(target.strip())})"
        except (ValueError, binascii.Error, OSError) as exc:
            warnings.append(f"An embedded image could not be decoded and was removed ({exc}).")
            return "*[Embedded image could not be decoded]*"

    md = MD_IMAGE_RE.sub(md_repl, md)
    md = HTML_IMG_RE.sub(html_repl, md)
    if count:
        warnings.insert(0, f"Docling embedded {count} image(s) as base64; they were extracted to files.")
    return md, count, warnings


# --------------------------------------------------------------------------- #
# LLM-friendly steps
# --------------------------------------------------------------------------- #
def normalize_i2c(md: str) -> Tuple[str, int]:
    total = 0

    def fix(line: str, _is_table: bool) -> str:
        nonlocal total
        new, n = _I2C_RE.subn("I²C", line)
        total += n
        return new

    return _map_prose_lines(md, fix), total


def remove_page_artifacts(md: str) -> Tuple[str, int]:
    """Remove standalone page-number lines and repeated running headers/footers.

    * A plain paragraph consisting only of "Page 3", "Page 3 of 40", "3 / 40",
      "- 3 -" is removed.
    * A short plain paragraph (<= 100 chars) that occurs at least 3 times and
      looks like a running header/footer (copyright, "Rev 1.2", URL, document
      number, "... Datasheet") keeps its first occurrence; repeats are removed.
      Sentences (ending with a period) are never treated as headers.
    """
    lines = md.split("\n")
    fence = None
    candidates: Counter = Counter()
    prose_idx: List[int] = []
    page_idx: List[int] = []
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if fence is not None:
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        s = line.strip()
        if s and not s.startswith(("|", "#")) and _PAGE_NUMBER_RE.match(s):
            page_idx.append(i)
        elif _is_plain_paragraph(line):
            prose_idx.append(i)
            candidates[s] += 1

    remove = set(page_idx)
    seen = set()
    for i in prose_idx:
        s = lines[i].strip()
        if (
            candidates[s] >= 3
            and len(s) <= 100
            and _FOOTER_KEYWORD_RE.search(s)
            and (not s.endswith(".") or "©" in s)
        ):
            if s in seen:
                remove.add(i)
            seen.add(s)
    if not remove:
        return md, 0
    return "\n".join(l for i, l in enumerate(lines) if i not in remove), len(remove)


def _caption_text(line: str) -> str:
    s = line.strip().strip("*_").strip()
    s = re.sub(r"\s+", " ", s)
    return s.replace("\\", "").replace("[", "(").replace("]", ")")


def add_caption_alt_text(md: str) -> Tuple[str, int]:
    """Use an adjacent "Figure x-y: ..." caption as the alt text of a generic image.

    The caption line itself stays in the text. Only images whose alt text is
    empty or the generic "Image" are changed.
    """
    lines = md.split("\n")
    n = len(lines)

    def neighbour(i: int, step: int) -> int:
        j, seen = i + step, 0
        while 0 <= j < n and seen < 3:
            if lines[j].strip():
                return j
            j += step
            seen += 1
        return -1

    fence = None
    changed = 0
    for i, line in enumerate(lines):
        m = _FENCE_RE.match(line)
        if fence is not None:
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        im = _STANDALONE_IMAGE_RE.match(line)
        if not im or im.group("alt").strip().lower() not in ("", "image"):
            continue
        caption = None
        prev = neighbour(i, -1)
        if prev >= 0 and _CAPTION_RE.match(lines[prev].strip()):
            caption = lines[prev]
        else:
            nxt = neighbour(i, +1)
            if nxt >= 0 and _CAPTION_RE.match(lines[nxt].strip()):
                after = neighbour(nxt, +1)
                # A caption directly followed by another image belongs to that image.
                if after < 0 or not _STANDALONE_IMAGE_RE.match(lines[after]):
                    caption = lines[nxt]
        if caption:
            alt = _caption_text(caption)
            if alt:
                lines[i] = line.replace(f"![{im.group('alt')}]", f"![{alt}]", 1)
                changed += 1
    return "\n".join(lines), changed


def normalize_whitespace(md: str) -> Tuple[str, int]:
    """Trim trailing spaces and collapse runs of blank lines (outside code fences)."""
    md = _map_prose_lines(md, lambda line, _t: line.rstrip())
    out: List[str] = []
    fence = None
    removed = 0
    for line in md.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None and m and m.group(1) == fence:
            fence = None
            out.append(line)
            continue
        if fence is None and not line.strip() and (not out or not out[-1].strip()):
            removed += 1
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
        removed += 1
    return "\n".join(out), removed


def basic_normalize(md: str) -> str:
    """Always applied: LF line endings, no BOM, single trailing newline."""
    md = md.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    return md.rstrip("\n") + "\n"


def clean_markdown(md: str, llm_friendly: bool = True) -> Tuple[str, CleanReport]:
    """Text cleanup after images are normalized. Never alters technical values."""
    report = CleanReport()
    md = basic_normalize(md)
    if llm_friendly:
        md, report.i2c_fixed = normalize_i2c(md)
        md, report.page_artifacts_removed = remove_page_artifacts(md)
        md, report.captions_added = add_caption_alt_text(md)
        md, report.blank_lines_removed = normalize_whitespace(md)
        md = basic_normalize(md)
    return md, report
