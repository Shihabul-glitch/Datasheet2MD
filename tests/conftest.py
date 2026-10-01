import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """Logs, settings and the saved file list go to a temp folder, never the user's %APPDATA%."""
    appdata = tmp_path / "_appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    return appdata


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    """A DoclingInfo whose worker is tests/fake_docling_worker.py (deterministic, no models)."""
    import core.docling_runner as dr

    monkeypatch.setattr(dr, "worker_script_path", lambda: HERE / "fake_docling_worker.py")
    log = tmp_path / "_fake_docling_calls.jsonl"
    monkeypatch.setenv("FAKE_DOCLING_LOG", str(log))
    info = dr.DoclingInfo(available=True, path="docling.exe", version="0.0-fake", python=sys.executable,
                          backend="worker", probe={"ocr_auto_engine": "rapidocr (fake)", "ocr_engines": {},
                                                   "torch": "0.0+cpu", "torch_cpu_only": True})

    def read(p):
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    info.calls = lambda: [json.loads(l) for l in read(log)]
    info.starts = lambda: len(read(Path(str(log) + ".starts")))
    return info
