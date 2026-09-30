import base64
from pathlib import Path

from core.docling_runner import (
    DoclingOptions,
    build_docling_args,
    explain_failure,
    interpret_line,
    parse_failed_count,
    pretty_log_text,
)
from core.validator import count_tables, sha256_file, validate_output

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# ------------------------------------------------------------------ runner
def test_args_default_no_ocr_and_list_form():
    args = build_docling_args("docling.exe", "source.pdf", ".", DoclingOptions(artifacts_path=Path("C:/m")))
    assert args[:3] == ["docling.exe", "convert", "source.pdf"]
    for flag in ("--no-ocr", "--tables", "-v"):
        assert flag in args
    assert args[args.index("--to") + 1] == "md"
    assert args[args.index("--image-export-mode") + 1] == "referenced"
    assert args[args.index("--table-mode") + 1] == "accurate"
    assert args[args.index("--pipeline") + 1] == "standard"
    assert args[args.index("--artifacts-path") + 1] == str(Path("C:/m"))
    assert "--ocr" not in args


def test_args_ocr_enabled():
    args = build_docling_args("d", "s.pdf", ".", DoclingOptions(ocr=True, debug=True))
    assert "--ocr" in args and args[args.index("--ocr-engine") + 1] == "auto"
    assert "--no-ocr" not in args and "-vv" in args and "--artifacts-path" not in args


def test_parse_failed_count():
    lines = ["x", "2026-09-30 18:09:22,630\tINFO\tdocling.cli.main: Processed 1 docs, of which 1 failed"]
    assert parse_failed_count(lines) == 1
    assert parse_failed_count(["nothing"]) is None


def test_interpret_line_filters_noise():
    assert not interpret_line("Loading weights:  41%|####      | 315/770 [00:00<00:00, 2783.15it/s]").show
    assert not interpret_line("HTTP Request: GET https://huggingface.co/api \"HTTP/1.1 200 OK\"").show
    ev = interpret_line("2026-09-30 17:55:29,413\tINFO\tdocling.pipeline.base_pipeline: Processing document source.pdf")
    assert ev.show and ev.stage
    w = interpret_line("2026-09-30 18:03:15,548\tWARNING\tdocling.models.stages.ocr.rapid_ocr_model: RapidOCR returned empty result!")
    assert w.show and w.level == "warning"
    assert pretty_log_text(w.text) == "WARNING: RapidOCR returned empty result!"


def test_explain_failure_patterns():
    assert "encrypted" in explain_failure(["pdfium: incorrect password"], 0)
    assert "disk is full" in explain_failure(["OSError: [Errno 28] No space left on device"], 1)
    assert "could not be opened" in explain_failure(["Input document source.pdf is not valid."], 0)
    assert "exit code 3" in explain_failure(["boom"], 3)


# ------------------------------------------------------------------ validator
def _make(tmp_path, md, images=("image_001.png",)):
    comp = tmp_path / "C"
    (comp / "images").mkdir(parents=True)
    for name in images:
        (comp / "images" / name).write_bytes(PNG)
    p = comp / "C.md"
    p.write_text(md, encoding="utf-8")
    return p, comp / "images"


def test_validation_passes(tmp_path):
    pdf = tmp_path / "C.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    h = sha256_file(pdf)
    md = "# T\n\n![Image](images/image_001.png)\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    p, imgs = _make(tmp_path, md)
    r = validate_output(p, imgs, pdf, h, expected_tables=1, expected_headings=1)
    assert r.passed, r.lines()
    assert r.image_count == 1 and r.table_count == 1
    assert "\u2713 1 image extracted" in r.lines()


def test_validation_fails_on_base64_absolute_and_missing(tmp_path):
    md = (
        "![a](data:image/png;base64,AAAA)\n![b](C:\\x\\image_001.png)\n"
        "![c](images/image_002.png)\n![d](images/image_001.png)\n"
    )
    p, imgs = _make(tmp_path, md)
    r = validate_output(p, imgs)
    assert not r.passed
    text = "\n".join(r.lines())
    assert "base64" in text and "absolute path" in text and "missing file" in text


def test_validation_numbering_order(tmp_path):
    md = "![a](images/image_002.png)\n![b](images/image_001.png)\n"
    p, imgs = _make(tmp_path, md, ("image_001.png", "image_002.png"))
    r = validate_output(p, imgs)
    assert any("document order" in c.message for c in r.checks if c.status == "fail")


def test_validation_detects_lost_tables(tmp_path):
    p, imgs = _make(tmp_path, "text\n", ())
    r = validate_output(p, imgs, expected_tables=2)
    assert not r.passed


def test_count_tables():
    md = "| a |\n|---|\n| 1 |\n\n```\n| x |\n|---|\n```\n\n|a|b|\n|:--|--:|\n"
    assert count_tables(md) == 2
