"""Datasheet2MD engine worker. Runs INSIDE Docling's own Python interpreter.

This file is executed as a standalone script by the Python that owns the
installed ``docling`` command (it never imports anything from Datasheet2MD).

``--probe``
    Print one JSON line describing the installation (versions, OCR engines,
    PyTorch build). Uses metadata/spec lookups only: fast, loads no models.

``--serve``
    Persistent worker for one batch. Reads one JSON request per line on stdin,
    writes ``D2MD_EVENT {json}`` lines on stdout (Docling's own log lines are
    passed through as plain lines). Docling and its models are loaded once and
    reused for the following documents; at most ONE converter (one set of
    models) is kept. Each document is converted in exactly one pass and only
    Markdown with referenced PNG images is written. After every document the
    result objects are released and the process memory is reported, so the app
    can recycle the worker when it grows too large.

    request:  {"op": "convert", "id": str, "workdir": str, "source": "source.pdf",
               "ocr": bool, "device": "cpu"|"cuda", "artifacts_path": str|null,
               "page_range": [first, last]|null, "num_threads": int|null}
              {"op": "exit"}
    events:   ready | stage {stage} | done {id, status, ...} | error {message}

Configuration (mirrors ``docling convert --pipeline standard --pdf-backend
docling_parse --tables --table-mode accurate --image-export-mode referenced``
as used by Datasheet2MD 1.0): standard PDF pipeline, threaded docling-parse
backend, TableFormer accurate with cell matching, picture images at scale 2,
OCR only when requested, every enrichment (formula, code, picture
classification/description, chart extraction) off.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
import traceback

PROTOCOL = 2


def _dist_version(name):
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def _has(module):
    try:
        import importlib.util

        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def emit(event, **data):
    data["event"] = event
    sys.stdout.write("D2MD_EVENT " + json.dumps(data) + "\n")
    sys.stdout.flush()


def rss_mb():
    """Resident memory of this process in MB (psutil if present, else Win32 API)."""
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 2**20)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                                     ctypes.byref(pmc), pmc.cb)
            return round(pmc.WorkingSetSize / 2**20)
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def _torch_build():
    """(version, cuda) read from torch/version.py without importing torch (fast).
    Package metadata alone can say "2.14.0" for a CPU-only build."""
    import re

    version = _dist_version("torch")
    cuda = None
    try:
        import importlib.util

        spec = importlib.util.find_spec("torch")
        for loc in (spec.submodule_search_locations or []) if spec else []:
            text = open(os.path.join(loc, "version.py"), encoding="utf-8").read()
            m = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)", text, re.M)
            version = m.group(1) if m else version
            m = re.search(r"^cuda(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
            cuda = m.group(1) if m else None
            break
    except Exception:
        pass
    return version, cuda


def probe():
    import shutil

    engines = {
        "rapidocr": _has("rapidocr"),
        "rapidocr_backend_onnxruntime": _has("onnxruntime"),
        "rapidocr_backend_torch": _has("torch"),
        "easyocr": _has("easyocr"),
        "tesserocr": _has("tesserocr"),
        "tesseract_cli": shutil.which("tesseract"),
    }
    # Mirrors the order of Docling's "auto" OCR engine on Windows.
    if engines["rapidocr"] and engines["rapidocr_backend_onnxruntime"]:
        auto = "rapidocr (onnxruntime)"
    elif engines["easyocr"]:
        auto = "easyocr"
    elif engines["rapidocr"] and engines["rapidocr_backend_torch"]:
        auto = "rapidocr (torch)"
    elif engines["tesserocr"] or engines["tesseract_cli"]:
        auto = "tesseract"
    else:
        auto = None
    torch_version, torch_cuda = _torch_build()
    info = {
        "probe_version": PROTOCOL,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "docling": _dist_version("docling"),
        "docling_core": _dist_version("docling-core"),
        "docling_parse": _dist_version("docling-parse"),
        "docling_ibm_models": _dist_version("docling-ibm-models"),
        "rapidocr_version": _dist_version("rapidocr"),
        "torch": torch_version,
        "torch_cuda": torch_cuda,  # CUDA version the PyTorch build targets, None for CPU-only builds
        "torch_cpu_only": bool(torch_version) and not torch_cuda,
        "psutil": _has("psutil"),
        "api_module": _has("docling.document_converter"),
        "cli_module": _has("docling.cli.main"),
        "ocr_engines": engines,
        "ocr_auto_engine": auto,
    }
    print("D2MD_PROBE " + json.dumps(info), flush=True)
    return 0


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self):
        self.key = None
        self.converter = None
        self.ocr_engine = None

    def _build(self, ocr, device, artifacts_path, num_threads):
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import OcrAutoOptions, PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        accel = AcceleratorOptions(device=device, **({"num_threads": num_threads} if num_threads else {}))
        opts = PdfPipelineOptions(
            accelerator_options=accel,
            do_ocr=bool(ocr),
            do_table_structure=True,
            do_code_enrichment=False,
            do_formula_enrichment=False,
            do_picture_classification=False,
            do_picture_description=False,
        )
        if hasattr(opts, "do_chart_extraction"):
            opts.do_chart_extraction = False
        if ocr:
            opts.ocr_options = OcrAutoOptions()
        tso = opts.table_structure_options
        if hasattr(tso, "mode"):
            tso.mode = TableFormerMode.ACCURATE
        if hasattr(tso, "do_cell_matching"):
            tso.do_cell_matching = True
        opts.generate_picture_images = True  # figures as PNG (same as the CLI's referenced mode)
        opts.generate_page_images = False    # no full-page PNGs inside the document object
        opts.images_scale = 2.0
        if artifacts_path:
            opts.artifacts_path = artifacts_path
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)},
        )
        converter.initialize_pipeline(InputFormat.PDF)  # load models now (honest "loading" stage)
        return converter

    def get(self, ocr, device, artifacts_path, num_threads):
        key = (bool(ocr), device, artifacts_path or "", num_threads or 0)
        if key != self.key:
            # Keep only one set of models: drop the previous converter first.
            self.converter = None
            self.key = None
            gc.collect()
            emit("stage", stage="loading_models", ocr=bool(ocr), device=device)
            t = time.monotonic()
            self.converter = self._build(ocr, device, artifacts_path, num_threads)
            self.key = key
            emit("models_loaded", seconds=round(time.monotonic() - t, 1), rss_mb=rss_mb())
        return self.converter


def _table_summary(doc):
    tables = []
    try:
        for t in doc.tables:
            cells = t.data.table_cells if t.data else []
            merged = sum(1 for c in cells if (c.row_span or 1) > 1 or (c.col_span or 1) > 1)
            page = t.prov[0].page_no if t.prov else None
            tables.append({"page": page, "rows": t.data.num_rows if t.data else 0,
                           "cols": t.data.num_cols if t.data else 0, "merged": merged})
    except Exception:
        pass
    return tables


def convert(engine, req):
    from docling.datamodel.base_models import ConversionStatus
    from docling_core.types.doc import ImageRefMode

    os.chdir(req["workdir"])  # relative --output-style paths -> relative image links
    device = req.get("device") or "cpu"
    if device.startswith("cuda"):
        import torch

        if not torch.cuda.is_available():
            emit("done", id=req["id"], status="device_unavailable",
                 errors=[f"CUDA is not available in Docling's PyTorch ({torch.__version__})."])
            return
    converter = engine.get(req.get("ocr"), device, req.get("artifacts_path"), req.get("num_threads"))
    emit("stage", stage="converting")
    kwargs = {"raises_on_error": False}
    if req.get("page_range"):
        kwargs["page_range"] = tuple(req["page_range"])
    t0 = time.monotonic()
    res = converter.convert(req["source"], **kwargs)
    t_convert = time.monotonic() - t0
    status = str(getattr(res.status, "value", res.status))
    errors = [getattr(e, "error_message", str(e)) for e in (getattr(res, "errors", None) or [])]
    pages = []
    try:
        pages = sorted(int(p.page_no) for p in res.pages)
    except Exception:
        pass
    page_count = getattr(getattr(res, "input", None), "page_count", None)
    tables = []
    t_export = 0.0
    md_written = False
    if res.status in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        emit("stage", stage="saving")
        t1 = time.monotonic()
        doc = res.document
        tables = _table_summary(doc)
        doc.save_as_markdown(filename=req.get("markdown", "source.md"), image_mode=ImageRefMode.REFERENCED)
        md_written = True
        t_export = time.monotonic() - t1
        del doc
    oom = any("out of memory" in e.lower() for e in errors)
    # Release this document's pages, images and backend before the next one.
    del res
    gc.collect()
    if device.startswith("cuda"):
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:
            pass
    emit("done", id=req["id"], status=status, errors=errors, pages=pages, page_count=page_count,
         tables=tables, markdown_written=md_written, cuda_oom=oom,
         timings={"convert_s": round(t_convert, 1), "export_s": round(t_export, 1)},
         rss_mb=rss_mb(), ocr_engine=engine.ocr_engine)


def serve():
    t = time.monotonic()
    import docling.document_converter  # noqa: F401  (import cost is paid once per worker)

    emit("ready", import_s=round(time.monotonic() - t, 1), rss_mb=rss_mb(),
         docling=_dist_version("docling"), torch=_dist_version("torch"), pid=os.getpid())
    engine = Engine()
    home = os.getcwd()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            emit("error", message="invalid request")
            continue
        if req.get("op") == "exit":
            break
        if req.get("op") != "convert":
            emit("error", message=f"unknown op {req.get('op')!r}")
            continue
        try:
            convert(engine, req)
        except BaseException as exc:  # report, keep the protocol alive
            if isinstance(exc, KeyboardInterrupt):
                raise
            traceback.print_exc()
            msg = f"{exc.__class__.__name__}: {exc}"
            emit("done", id=req.get("id"), status="failure", errors=[msg],
                 cuda_oom="out of memory" in msg.lower(), rss_mb=rss_mb())
            gc.collect()
        finally:
            # Never keep a job's staging folder as the working directory: Windows
            # cannot delete a folder that is some process's current directory.
            os.chdir(home)
    return 0


def main(argv):
    if argv[:1] == ["--probe"]:
        return probe()
    if argv[:1] == ["--serve"]:
        import logging

        logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                            format="%(asctime)s\t%(levelname)s\t%(name)s: %(message)s")
        for noisy in ("httpx", "urllib3", "huggingface_hub", "filelock", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        return serve()
    print("usage: docling_worker.py --probe | --serve", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
