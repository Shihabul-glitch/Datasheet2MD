import base64
import sys
from pathlib import Path

from core.docling_runner import (
    DoclingOptions,
    build_docling_args,
    explain_failure,
    find_docling_python,
    interpret_line,
    parse_failed_count,
    pretty_log_text,
    probe_worker,
)
from core.validator import count_tables, little_text_warning, sha256_file, validate_output

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# ------------------------------------------------------------------ CLI fallback arguments
def test_cli_args_default_no_ocr_markdown_only():
    args = build_docling_args("docling.exe", "source.pdf", ".", DoclingOptions(artifacts_path=Path("C:/m")))
    assert args[:3] == ["docling.exe", "convert", "source.pdf"]
    assert "--no-ocr" in args and "--ocr" not in args and "--tables" in args
    assert args.count("--to") == 1 and args[args.index("--to") + 1] == "md"  # Markdown only (no JSON/HTML)
    assert args[args.index("--image-export-mode") + 1] == "referenced"
    assert args[args.index("--table-mode") + 1] == "accurate"
    assert args[args.index("--pipeline") + 1] == "standard"
    assert args[args.index("--artifacts-path") + 1] == str(Path("C:/m"))
    for removed in ("--ocr-mode", "--document-timeout", "--enrich-formula", "--enrich-picture-classes"):
        assert removed not in args


def test_cli_args_ocr_enabled():
    args = build_docling_args("d", "s.pdf", ".", DoclingOptions(ocr=True, debug=True, device="cuda"))
    assert "--ocr" in args and args[args.index("--ocr-engine") + 1] == "auto"
    assert "--no-ocr" not in args and "-vv" in args and args[args.index("--device") + 1] == "cuda"


# ------------------------------------------------------------------ worker script
def test_worker_script_defaults_match_v1_settings():
    """The real worker builds: standard pipeline, accurate tables, OCR only on request,
    every enrichment off, referenced PNG pictures - checked in its source."""
    src = (Path(__file__).parents[1] / "core" / "docling_worker.py").read_text(encoding="utf-8")
    for needle in ("do_ocr=bool(ocr)", "do_formula_enrichment=False", "do_picture_classification=False",
                   "do_picture_description=False", "do_code_enrichment=False", "TableFormerMode.ACCURATE",
                   "generate_picture_images = True", "ImageRefMode.REFERENCED", "save_as_markdown"):
        assert needle in src, needle
    for absent in ("save_as_json", "save_as_html", "export_to_html"):
        assert absent not in src


def test_worker_probe_runs_in_this_interpreter():
    info = probe_worker(sys.executable, 60)
    assert info["python_version"] and "ocr_engines" in info and "torch_cpu_only" in info


def test_find_docling_python_from_launcher_shebang(tmp_path):
    py = tmp_path / "Python310" / "python.exe"
    py.parent.mkdir()
    py.write_bytes(b"MZ")
    launcher = tmp_path / "Scripts" / "docling.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"MZ\x00launcher\x00" + b"#!" + str(py).encode() + b"\r\nPK\x03\x04zip")
    assert find_docling_python(str(launcher)) == str(py)
    assert find_docling_python(None) is None


# ------------------------------------------------------------------ log handling
def test_parse_failed_count():
    lines = ["x", "2026-09-30 18:09:22,630\tINFO\tdocling.cli.main: Processed 1 docs, of which 1 failed"]
    assert parse_failed_count(lines) == 1
    assert parse_failed_count(["nothing"]) is None


def test_log_lines_never_drive_the_stage():
    """v2.0 showed 'Loading OCR models' for the whole conversion because RapidOCR's
    'returned empty result' warnings matched a stage pattern. Stages now come only
    from explicit worker events; such lines are demoted to debug."""
    ev = interpret_line("2026-10-01 10:46:36,302\tWARNING\tdocling.models.stages.ocr.rapid_ocr_model: "
                        "RapidOCR returned empty result!")
    assert not hasattr(ev, "stage") and ev.level == "debug"
    assert not interpret_line("Loading weights:  41%|####      | 315/770 [00:00<00:00, 2783.15it/s]").show
    w = interpret_line("2026-09-30 18:03:15,548\tWARNING\tdocling.foo: Something odd")
    assert w.show and w.level == "warning" and pretty_log_text(w.text) == "WARNING: Something odd"
    assert "\x1b" not in interpret_line("\x1b[32m[INFO] x\x1b[0m").text


def test_explain_failure_patterns():
    assert "encrypted" in explain_failure(["pdfium: incorrect password"], 0)
    assert "disk is full" in explain_failure(["OSError: [Errno 28] No space left on device"], 1)
    assert "could not be opened" in explain_failure(["Input document source.pdf is not valid."], 0)
    assert "out of memory" in explain_failure(["CUDA out of memory. Tried to allocate"], 1)
    assert "models" in explain_failure(["LocalEntryNotFoundError: x"], 1)
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


def test_validation_fails_on_base64_absolute_missing_and_escaping(tmp_path):
    (tmp_path / "outside.png").write_bytes(PNG)
    md = ("![a](data:image/png;base64,AAAA)\n![b](C:\\x\\image_001.png)\n"
          "![c](images/image_002.png)\n![d](images/image_001.png)\n![e](../outside.png)\n")
    p, imgs = _make(tmp_path, md)
    r = validate_output(p, imgs)
    assert not r.passed
    text = "\n".join(r.lines())
    for needle in ("base64", "absolute path", "missing file", "outside the document folder"):
        assert needle in text


def test_validation_detects_lost_tables_and_bad_utf8(tmp_path):
    p, imgs = _make(tmp_path, "text\n", ())
    assert not validate_output(p, imgs, expected_tables=2).passed
    p.write_bytes(b"\xff\xfe broken")
    assert not validate_output(p, imgs).passed


def test_source_change_detected(tmp_path):
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1")
    h = sha256_file(pdf)
    pdf.write_bytes(b"%PDF-2")
    p, imgs = _make(tmp_path, "text\n", ())
    assert not validate_output(p, imgs, pdf, h).passed


def test_count_tables():
    md = "| a |\n|---|\n| 1 |\n\n```\n| x |\n|---|\n```\n\n|a|b|\n|:--|--:|\n"
    assert count_tables(md) == 2


def test_little_text_warning_is_a_hint_only_without_ocr():
    assert little_text_warning(100, 5, ocr_enabled=False)
    assert little_text_warning(100, 5, ocr_enabled=True) is None
    assert little_text_warning(5000, 5, ocr_enabled=False) is None
