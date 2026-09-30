"""Collect Docling's exported images into ``images/image_###.png`` and relink them.

Images are numbered in order of first appearance in the Markdown. Every image
reference is rewritten to a relative, forward-slash path such as
``images/image_001.png``. A reference whose file cannot be found is replaced by
a visible text placeholder (and reported) so no broken link is ever written.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

# ![alt](target "title") - target may be <...>, may contain one level of (...),
# and (non-standard, but written by some tools for Windows paths) may contain spaces.
MD_IMAGE_RE = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\\\]\n])*)\]"
    r"\(\s*(?P<target><[^>\n]*>|(?:[^()\s]|\([^()\s]*\))+|[^()\n\"']+?)"
    r"(?P<title>\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)"
)
HTML_IMG_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?P<q>[\"'])(?P<target>.*?)(?P=q)[^>]*>", re.IGNORECASE | re.DOTALL
)
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageError(Exception):
    pass


@dataclass
class ImageRef:
    start: int
    end: int
    kind: str  # "md" or "html"
    alt: str
    target: str
    title: str = ""


@dataclass
class ImageResult:
    markdown: str
    image_files: List[Path] = field(default_factory=list)  # in numbering order
    missing: List[str] = field(default_factory=list)
    remote: List[str] = field(default_factory=list)
    duplicates: int = 0

    @property
    def count(self) -> int:
        return len(self.image_files)


def fenced_code_spans(md: str) -> List[Tuple[int, int]]:
    """Character spans of fenced code blocks (their content is never touched)."""
    spans, pos, start, fence = [], 0, None, None
    for line in md.splitlines(keepends=True):
        m = _FENCE_RE.match(line)
        if m:
            if start is None:
                start, fence = pos, m.group(1)
            elif m.group(1) == fence:
                spans.append((start, pos + len(line)))
                start, fence = None, None
        pos += len(line)
    if start is not None:
        spans.append((start, len(md)))
    return spans


def _in_spans(i: int, spans: List[Tuple[int, int]]) -> bool:
    return any(a <= i < b for a, b in spans)


def iter_image_refs(md: str) -> Iterator[ImageRef]:
    """All Markdown and HTML image references outside code fences, in document order."""
    spans = fenced_code_spans(md)
    refs = [
        ImageRef(m.start(), m.end(), "md", m.group("alt"), m.group("target"), m.group("title") or "")
        for m in MD_IMAGE_RE.finditer(md)
    ]
    refs += [
        ImageRef(m.start(), m.end(), "html", _html_alt(m.group(0)), m.group("target"))
        for m in HTML_IMG_RE.finditer(md)
    ]
    for ref in sorted(refs, key=lambda r: r.start):
        if not _in_spans(ref.start, spans):
            yield ref


def _html_alt(tag: str) -> str:
    m = re.search(r"\balt\s*=\s*([\"'])(.*?)\1", tag, re.IGNORECASE | re.DOTALL)
    return m.group(2) if m else "Image"


def clean_target(target: str) -> str:
    t = target.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    return t


def is_data_uri(target: str) -> bool:
    return clean_target(target).lower().startswith("data:")


def is_remote(target: str) -> bool:
    t = clean_target(target)
    if re.match(r"^[a-zA-Z]:[\\/]", t):  # Windows drive path, not a scheme
        return False
    return bool(_SCHEME_RE.match(t)) and not t.lower().startswith(("file:", "data:"))


def resolve_local_target(target: str, base_dir: Path) -> Optional[Path]:
    """Resolve a local image reference (relative, absolute, file:// or %-encoded)."""
    t = clean_target(target)
    if not t or is_data_uri(t) or is_remote(t):
        return None
    candidates: List[str] = []
    if t.lower().startswith("file:"):
        parsed = urlparse(t)
        path = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = "\\\\" + parsed.netloc + path  # UNC
        elif re.match(r"^[\\/][a-zA-Z]:", path):
            path = path[1:]
        candidates.append(path)
    else:
        candidates += [t, unquote(t)]
    for c in candidates:
        p = Path(c)
        if not p.is_absolute():
            p = base_dir / p
        if p.is_file():
            return p.resolve()
    return None


def _detect_extension(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff"):
        return ".jpg" if ext == ".jpeg" else ext
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return ".png"
    if head.startswith(_PNG_SIGNATURE):
        return ".png"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def image_name(n: int, width: int, ext: str = ".png") -> str:
    return f"image_{n:0{width}d}{ext}"


def _escape_alt(alt: str) -> str:
    return alt.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def normalize_images(md: str, base_dir: Path, images_dir: Path, rel_prefix: str = "images") -> ImageResult:
    """Copy referenced images to ``images_dir`` as image_001.png... and relink them.

    ``base_dir`` is the directory relative references are resolved against
    (Docling's output directory). Files are copied, never moved, so a failure
    part-way leaves the source files intact.
    """
    refs = list(iter_image_refs(md))
    order: Dict[str, Path] = {}
    for ref in refs:
        p = resolve_local_target(ref.target, base_dir)
        if p is not None:
            order.setdefault(os.path.normcase(str(p)), p)
    width = max(3, len(str(len(order))))

    result = ImageResult(markdown=md)
    names: Dict[str, str] = {}
    if order:
        try:
            images_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ImageError(f"Cannot create images folder '{images_dir}': {exc}") from exc
    for n, (key, src) in enumerate(order.items(), start=1):
        name = image_name(n, width, _detect_extension(src))
        dest = images_dir / name
        try:
            shutil.copyfile(src, dest)
        except OSError as exc:
            raise ImageError(f"Cannot copy image '{src.name}' to '{dest}': {exc}") from exc
        names[key] = name
        result.image_files.append(dest)

    out: List[str] = []
    pos = 0
    seen = set()
    for ref in refs:
        out.append(md[pos:ref.start])
        pos = ref.end
        original = md[ref.start:ref.end]
        if is_data_uri(ref.target):
            out.append(original)  # handled by markdown_cleaner.extract_embedded_images
            continue
        if is_remote(ref.target):
            result.remote.append(clean_target(ref.target))
            out.append(original)
            continue
        p = resolve_local_target(ref.target, base_dir)
        alt = ref.alt if ref.kind == "md" else _escape_alt(ref.alt)
        if p is None:
            result.missing.append(clean_target(ref.target))
            label = (ref.alt or "Image").replace("\\", "")
            out.append(f"*[Image missing: {label}]*")
            continue
        key = os.path.normcase(str(p))
        if key in seen:
            result.duplicates += 1
        seen.add(key)
        new_target = f"{rel_prefix}/{names[key]}" if rel_prefix else names[key]
        out.append(f"![{alt}]({new_target}{ref.title})")
    out.append(md[pos:])
    result.markdown = "".join(out)
    return result


def local_image_targets(md: str) -> List[str]:
    """Local (non-remote, non-data) image targets in document order."""
    return [
        clean_target(r.target)
        for r in iter_image_refs(md)
        if not is_remote(r.target) and not is_data_uri(r.target)
    ]


def is_png(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(8) == _PNG_SIGNATURE
    except OSError:
        return False
