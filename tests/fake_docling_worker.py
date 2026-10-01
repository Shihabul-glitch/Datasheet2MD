"""Stand-in for core/docling_worker.py (--serve protocol), used by the tests.

Writes what Docling writes for Markdown export (source.md + source_artifacts/*.png)
and speaks the same JSON-lines protocol. Behaviour is selected by markers
embedded in the PDF:

    FAKE-FAIL        status "failure"
    FAKE-PARTIAL     status "partial_success", last page missing (Markdown still written)
    FAKE-SLOW        sleeps FAKE_SLOW_SECONDS (default 30 s) before converting
    FAKE-CRASH       the worker process exits abruptly
    FAKE-OOM         on device "cuda": CUDA out-of-memory failure; on "cpu": normal
    FAKE-IMAGES-<n>  number of images (default 2)
    FAKE-MERGED      one table with a merged cell
    FAKE-NOTEXT      almost no text in the Markdown

Every request is appended to $FAKE_DOCLING_LOG (JSON lines) and each worker
start to $FAKE_DOCLING_LOG + ".starts". $FAKE_RSS_MB sets the reported memory.
This exercises orchestration only - it proves nothing about extraction quality.
"""

import base64
import json
import os
import re
import sys
import time

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def emit(event, **data):
    data["event"] = event
    sys.stdout.write("D2MD_EVENT " + json.dumps(data) + "\n")
    sys.stdout.flush()


def convert(req, state):
    os.chdir(req["workdir"])
    log = os.environ.get("FAKE_DOCLING_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(req) + "\n")
    data = open(req["source"], "rb").read()
    key = (req.get("ocr"), req.get("device"))
    if key != state.get("key"):
        emit("stage", stage="loading_models")
        emit("models_loaded", seconds=0.1)
        state["key"] = key
        if req.get("ocr"):
            print("INFO docling.models.stages.ocr.auto_ocr_model: Auto OCR model selected rapidocr with torch.",
                  flush=True)
    emit("stage", stage="converting")
    print("INFO docling.pipeline.base_pipeline: Processing document source.pdf", flush=True)
    if b"FAKE-SLOW" in data:
        for _ in range(int(float(os.environ.get("FAKE_SLOW_SECONDS", "30")) * 10)):
            time.sleep(0.1)
    if b"FAKE-CRASH" in data:
        os._exit(3)
    count = int(re.search(rb"/Count (\d+)", data).group(1))
    rng = req.get("page_range")
    pages = list(range(rng[0], min(rng[1], count) + 1)) if rng else list(range(1, count + 1))
    rss = int(os.environ.get("FAKE_RSS_MB", "900"))
    if b"FAKE-OOM" in data and str(req.get("device")).startswith("cuda"):
        emit("done", id=req["id"], status="failure", errors=["CUDA out of memory. Tried to allocate 20 MiB"],
             cuda_oom=True, rss_mb=rss)
        return
    if b"FAKE-FAIL" in data:
        emit("done", id=req["id"], status="failure", errors=["Input document source.pdf is not valid."],
             pages=[], page_count=count, rss_mb=rss)
        return
    status, errors = "success", []
    if b"FAKE-PARTIAL" in data:
        status, errors = "partial_success", [f"Page {pages[-1]} failed"]
        pages = pages[:-1]
    texts = [t.decode("latin-1") for t in re.findall(rb"\((.*?)\) Tj", data)]
    if b"FAKE-NOTEXT" in data:
        texts = ["x"]
    m = re.search(rb"FAKE-IMAGES-(\d+)", data)
    n_images = int(m.group(1)) if m else 2
    emit("stage", stage="saving")
    os.makedirs("source_artifacts", exist_ok=True)
    md = [f"# {req['source']}", ""]
    for t in texts:
        md += [t, ""]
    for i in range(n_images):
        name = f"image_{i:06d}_{'ab' * 8}{i}.png"
        with open(os.path.join("source_artifacts", name), "wb") as fh:
            fh.write(PNG + bytes([i]))
        md += [f"Figure 1-{i + 1}: Test figure {i + 1}", "", f"![Image](source_artifacts/{name})", ""]
    md += ["| Parameter | Min | Max |", "|---|---|---|", "| VDD | 2.4 | 3.6 |", ""]
    with open(req.get("markdown", "source.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    tables = [{"page": 1, "rows": 2, "cols": 3, "merged": 1 if b"FAKE-MERGED" in data else 0}]
    emit("done", id=req["id"], status=status, errors=errors, pages=pages, page_count=count, tables=tables,
         markdown_written=True, timings={"convert_s": 0.1, "export_s": 0.0}, rss_mb=rss)


def main(argv):
    if argv[:1] == ["--probe"]:
        print("D2MD_PROBE " + json.dumps({"docling": "0.0-fake", "api_module": True, "torch": "0.0+cpu",
                                          "torch_cpu_only": True, "ocr_auto_engine": "rapidocr (fake)",
                                          "ocr_engines": {}}), flush=True)
        return 0
    log = os.environ.get("FAKE_DOCLING_LOG")
    if log:
        with open(log + ".starts", "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
    emit("ready", import_s=0.0, rss_mb=100)
    state = {}
    home = os.getcwd()
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("op") == "exit":
            break
        try:
            convert(req, state)
        finally:
            os.chdir(home)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
