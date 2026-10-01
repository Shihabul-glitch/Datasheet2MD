"""Tiny, dependency-free generator of real (parseable) PDFs (GPU self-test and tests).

Page kinds:
    {"text": "..."}   one or more lines of Helvetica text (a real text layer)
    {"scan": True}    no text, one raster image covering the whole page
    {"blank": True}   nothing on the page
Extra ``marker`` strings are embedded as PDF comments; the fake Docling worker
reads them to decide how to behave (fail, partial, slow...).
"""

from __future__ import annotations

from pathlib import Path
from typing import List


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, pages: List[dict], marker: str = "") -> Path:
    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # placeholder, filled below
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    image = add(b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
                b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream")
    kids = []
    for page in pages:
        if page.get("text"):
            lines = page["text"].split("\n")
            ops = ["BT /F1 11 Tf 14 TL 50 760 Td"]
            for line in lines:
                ops.append(f"({_esc(line)}) Tj T*")
            ops.append("ET")
            content = "\n".join(ops).encode("latin-1")
        elif page.get("scan"):
            content = b"q 612 0 0 792 0 0 cm /Im1 Do Q"
        else:
            content = b""
        c = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        p = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                b"/Resources << /Font << /F1 %d 0 R >> /XObject << /Im1 %d 0 R >> >> >>"
                % (pages_obj, c, font, image))
        kids.append(p)
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                              + b"] /Count %d >>" % len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    if marker:
        out += f"% {marker}\n".encode("ascii")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def text_pdf(path: Path, title: str, n_pages: int = 2, marker: str = "") -> Path:
    body = "\n".join(f"{title} line {i}: VDD 3.3 V, register 0x{i:02X}, pin {i}" for i in range(12))
    return make_pdf(path, [{"text": f"{title} page {n}\n{body}"} for n in range(1, n_pages + 1)], marker)
