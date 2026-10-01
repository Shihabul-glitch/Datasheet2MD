"""Batch/converter integration tests with a fake Docling worker.

Real PDF files, real subprocesses, the real persistent-worker protocol. They
test orchestration, output layout and safety - NOT extraction accuracy (the
engine is a stand-in); see DEVELOPER_REPORT.md for real-Docling checks.
"""

import json
import time
from pathlib import Path

import pytest

from core.batch import INTERRUPTED, PENDING, RUNNING, BatchOptions, BatchQueue, BatchRunner, batch_summary
from core.config import Settings
from core.converter import CANCELLED, COMPLETED, FAILED, PARTIAL, WARNINGS
from core.sample_pdf import make_pdf, text_pdf
from core.validator import validate_output


def run_batch(queue, engine, **kw):
    runner = BatchRunner(queue, engine, BatchOptions(**kw))
    summary = runner.run()
    return runner, summary


def folders(root: Path):
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))


def assert_document_folder(folder: Path, md_name: str, n_images: int) -> str:
    """Exactly <name>.md + images/image_###.png - nothing else in the folder."""
    entries = sorted(p.name for p in folder.iterdir())
    assert entries == sorted([md_name] + (["images"] if n_images else [])), entries
    text = (folder / md_name).read_text(encoding="utf-8")
    assert "data:image" not in text and "source_artifacts" not in text and ":\\" not in text
    if n_images:
        assert [p.name for p in sorted((folder / "images").iterdir())] == [f"image_{i:03d}.png"
                                                                           for i in range(1, n_images + 1)]
        for i in range(1, n_images + 1):
            assert f"](images/image_{i:03d}.png)" in text
    assert validate_output(folder / md_name, folder / "images").passed
    return text


def test_ocr_is_off_by_default_and_on_only_when_requested(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / "a.pdf", "A")])
    run_batch(q, fake_engine, output_root=tmp_path / "out")
    assert fake_engine.calls()[0]["ocr"] is False
    assert BatchOptions().ocr is False and Settings().ocr_enabled is False
    q2 = BatchQueue()
    q2.add_paths([text_pdf(tmp_path / "b.pdf", "B")])
    run_batch(q2, fake_engine, output_root=tmp_path / "out", ocr=True)
    assert fake_engine.calls()[-1]["ocr"] is True


def test_several_pdfs_separate_folders_one_pass_each_one_worker(tmp_path, fake_engine):
    src = tmp_path / "in"
    text_pdf(src / "dirA" / "LM358-Datasheet.pdf", "Alpha", marker="FAKE-IMAGES-2")
    text_pdf(src / "dirB" / "LM358-Datasheet.pdf", "Bravo", marker="FAKE-IMAGES-3")
    text_pdf(src / "Ünïcode dir with spaces" / "µ Sensor (rev B).pdf", "Charlie", marker="FAKE-IMAGES-1")
    q = BatchQueue()
    assert len(q.add_paths([src], recursive=True).added) == 3
    out = tmp_path / "out root"
    runner, summary = run_batch(q, fake_engine, output_root=out)
    assert summary["succeeded"] == 3, summary
    assert folders(out) == ["LM358", "LM358_2", "µ Sensor (rev B)"]
    ta = assert_document_folder(out / "LM358", "LM358-Datasheet.md", 2)
    tb = assert_document_folder(out / "LM358_2", "LM358-Datasheet.md", 3)
    tc = assert_document_folder(out / "µ Sensor (rev B)", "µ Sensor (rev B).md", 1)
    assert "Alpha" in ta and "Bravo" in tb and "Charlie" in tc and "Bravo" not in ta
    assert len(fake_engine.calls()) == 3  # exactly one conversion pass per PDF
    assert fake_engine.starts() == 1  # models loaded once for the whole batch
    assert not runner.engine.alive  # worker shut down at the end of the batch
    assert not (out / ".d2md_staging").exists()


def test_beside_source_and_no_extra_files(tmp_path, fake_engine):
    pdf = text_pdf(tmp_path / "docs" / "ADS1115_Datasheet.pdf", "Beside", marker="FAKE-MERGED")
    q = BatchQueue()
    q.add_paths([pdf])
    run_batch(q, fake_engine)
    assert_document_folder(tmp_path / "docs" / "ADS1115", "ADS1115_Datasheet.md", 2)
    assert sorted(p.name for p in (tmp_path / "docs").iterdir()) == ["ADS1115", "ADS1115_Datasheet.pdf"]
    # the merged-cell table is reported as a warning, not as an HTML side file
    assert q.jobs[0].status == WARNINGS and any("merged cells" in w for w in q.jobs[0].warnings)


def test_dedupe_and_folder_scan(tmp_path):
    a = text_pdf(tmp_path / "x" / "chip.pdf", "A")
    b = text_pdf(tmp_path / "y" / "chip.pdf", "B")
    q = BatchQueue()
    assert len(q.add_paths([a, b]).added) == 2
    again = q.add_paths([str(a).upper(), tmp_path / "x" / ".." / "x" / "chip.pdf", tmp_path / "x"])
    assert len(again.added) == 0 and len(again.duplicates) == 3
    (tmp_path / "notes.txt").write_text("x")
    other = q.add_paths([tmp_path / "notes.txt", tmp_path / "missing.pdf"])
    assert other.not_pdf and other.missing
    text_pdf(tmp_path / "sub" / "deep.PDF", "D")
    assert len(BatchQueue().add_paths([tmp_path / "sub"], recursive=False).added) == 1
    assert len(BatchQueue().add_paths([tmp_path], recursive=False).added) == 0
    assert len(BatchQueue().add_paths([tmp_path], recursive=True).added) == 3


def test_existing_output_gets_new_version_and_user_files_untouched(tmp_path, fake_engine):
    pdf = text_pdf(tmp_path / "LM358.pdf", "One")
    existing = tmp_path / "LM358"
    existing.mkdir()
    (existing / "my notes.md").write_text("user file")
    for _ in range(2):
        q = BatchQueue()
        q.add_paths([pdf])
        run_batch(q, fake_engine)
    assert sorted(p.name for p in existing.iterdir()) == ["my notes.md"]
    assert (existing / "my notes.md").read_text() == "user file"
    assert_document_folder(tmp_path / "LM358_2", "LM358.md", 2)
    assert_document_folder(tmp_path / "LM358_3", "LM358.md", 2)


def test_failed_job_does_not_stop_the_batch(tmp_path, fake_engine):
    bad = text_pdf(tmp_path / "a_bad.pdf", "Bad", marker="FAKE-FAIL")
    good = text_pdf(tmp_path / "b_good.pdf", "Good")
    corrupt = tmp_path / "c_corrupt.pdf"
    corrupt.write_bytes(b"not a pdf at all")
    q = BatchQueue()
    q.add_paths([bad, good, corrupt])
    _, summary = run_batch(q, fake_engine, output_root=tmp_path / "out")
    assert [j.status for j in q.jobs] == [FAILED, COMPLETED, FAILED]
    assert "could not be opened" in q.jobs[0].error
    assert "not a valid PDF" in q.jobs[2].error
    assert folders(tmp_path / "out") == ["b_good"]
    assert len(fake_engine.calls()) == 2  # no retries; the corrupt file never reached the engine


def test_worker_crash_is_a_failure_and_next_file_still_converts(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / "a.pdf", "A", marker="FAKE-CRASH"), text_pdf(tmp_path / "b.pdf", "B")])
    run_batch(q, fake_engine, output_root=tmp_path / "out")
    assert [j.status for j in q.jobs] == [FAILED, COMPLETED]
    assert "stopped unexpectedly" in q.jobs[0].error
    assert fake_engine.starts() == 2


def test_partial_conversion_is_not_success_and_never_replaces_output(tmp_path, fake_engine):
    pdf = tmp_path / "LM358.pdf"
    text_pdf(pdf, "Good", n_pages=3)
    q = BatchQueue()
    q.add_paths([pdf])
    run_batch(q, fake_engine)
    good = (tmp_path / "LM358" / "LM358.md").read_text(encoding="utf-8")
    text_pdf(pdf, "Broken", n_pages=3, marker="FAKE-PARTIAL")
    q2 = BatchQueue()
    q2.add_paths([pdf])
    _, summary = run_batch(q2, fake_engine)
    job = q2.jobs[0]
    assert job.status == PARTIAL and summary["succeeded"] == 0 and summary["failed"] == 1
    assert (tmp_path / "LM358" / "LM358.md").read_text(encoding="utf-8") == good
    assert job.output_dir.endswith("LM358_incomplete")
    assert "PARTIAL" in job.error and "page(s) missing" in job.error


def _wait_for(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_cancel_current_kills_worker_and_continues(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / "a_slow.pdf", "S", marker="FAKE-SLOW"), text_pdf(tmp_path / "b.pdf", "B")])
    runner = BatchRunner(q, fake_engine, BatchOptions(output_root=tmp_path / "out"))
    runner.start()
    assert _wait_for(lambda: len(fake_engine.calls()) == 1)
    runner.cancel_current()
    runner.join(30)
    assert not runner.is_alive()
    assert [j.status for j in q.jobs] == [CANCELLED, COMPLETED], q.jobs[1].error
    assert folders(tmp_path / "out") == ["b"]  # nothing published for the cancelled file
    assert fake_engine.starts() == 2  # killed worker replaced by a fresh one
    assert not (tmp_path / "out" / ".d2md_staging").exists()


def test_stop_batch_starts_nothing_else(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / "a_slow.pdf", "S", marker="FAKE-SLOW"), text_pdf(tmp_path / "b.pdf", "B")])
    runner = BatchRunner(q, fake_engine, BatchOptions(output_root=tmp_path / "out"))
    runner.start()
    assert _wait_for(lambda: len(fake_engine.calls()) == 1)
    runner.stop()
    runner.join(30)
    assert [j.status for j in q.jobs] == [CANCELLED, PENDING]
    assert len(fake_engine.calls()) == 1
    assert not runner.engine.alive


def test_worker_recycled_when_memory_limit_exceeded(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("FAKE_RSS_MB", "5000")
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / f"{n}.pdf", n) for n in "abc"])
    run_batch(q, fake_engine, output_root=tmp_path / "out", recycle_mb=2000)
    assert all(j.status == COMPLETED for j in q.jobs)
    assert fake_engine.starts() == 3


def test_gpu_out_of_memory_falls_back_to_cpu(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / "g.pdf", "G", marker="FAKE-OOM")])
    run_batch(q, fake_engine, output_root=tmp_path / "out", device="cuda")
    assert [c["device"] for c in fake_engine.calls()] == ["cuda", "cpu"]
    job = q.jobs[0]
    assert job.status == WARNINGS and any("GPU ran out of memory" in w for w in job.warnings)


def test_little_text_warns_and_suggests_ocr_without_rerunning(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([make_pdf(tmp_path / "scan.pdf", [{"scan": True}, {"scan": True}], marker="FAKE-NOTEXT")])
    run_batch(q, fake_engine, output_root=tmp_path / "out")
    job = q.jobs[0]
    assert job.status == WARNINGS and any("Enable OCR" in w for w in job.warnings)
    assert len(fake_engine.calls()) == 1 and fake_engine.calls()[0]["ocr"] is False
    q2 = BatchQueue()
    q2.add_paths([make_pdf(tmp_path / "scan2.pdf", [{"scan": True}], marker="FAKE-NOTEXT")])
    run_batch(q2, fake_engine, output_root=tmp_path / "out", ocr=True)
    assert not any("Enable OCR" in w for w in q2.jobs[0].warnings)


def test_page_range_and_source_unchanged(tmp_path, fake_engine):
    pdf = text_pdf(tmp_path / "p.pdf", "P", n_pages=5)
    before = pdf.read_bytes()
    q = BatchQueue()
    q.add_paths([pdf])
    run_batch(q, fake_engine, page_range="2-3")
    assert fake_engine.calls()[0]["page_range"] == [2, 3]
    assert q.jobs[0].status == COMPLETED and pdf.read_bytes() == before


def test_queue_state_recovery(tmp_path):
    state = tmp_path / "queue.json"
    q = BatchQueue(state)
    q.add_paths([text_pdf(tmp_path / "a.pdf", "A"), text_pdf(tmp_path / "b.pdf", "B"),
                 text_pdf(tmp_path / "c.pdf", "C")])
    q.jobs[0].status = COMPLETED
    q.jobs[1].status = RUNNING
    q.save()
    restored = BatchQueue.load(state)
    assert [j.status for j in restored.jobs] == [COMPLETED, INTERRUPTED, PENDING]
    assert restored.next_runnable().id == restored.jobs[1].id  # completed files are not converted again
    state.write_text("{not json", encoding="utf-8")
    assert BatchQueue.load(state).jobs == []
    # a v2.0 list with removed statuses/fields still loads
    state.write_text(json.dumps({"jobs": [{"source": "x.pdf", "status": "skipped", "ocr_summary": "x"}]}))
    assert [j.status for j in BatchQueue.load(state).jobs] == [COMPLETED]


def test_batch_summary(tmp_path, fake_engine):
    q = BatchQueue()
    q.add_paths([text_pdf(tmp_path / "a.pdf", "A"), text_pdf(tmp_path / "b.pdf", "B", marker="FAKE-FAIL")])
    run_batch(q, fake_engine, output_root=tmp_path / "out")
    s = batch_summary(q.snapshot())
    assert s["succeeded"] == 1 and s["failed"] == 1 and len(s["jobs"]) == 2
    assert "convert_s" in s["jobs"][0]["timings"]


def test_cli_exit_codes_and_removed_options(tmp_path, fake_engine, monkeypatch, capsys):
    import core.docling_runner as dr
    import main

    monkeypatch.setattr(dr, "detect_docling", lambda *a, **k: fake_engine)
    good = text_pdf(tmp_path / "good.pdf", "G")
    bad = text_pdf(tmp_path / "bad.pdf", "B", marker="FAKE-FAIL")
    report = tmp_path / "rep.json"
    assert main.main(["--cli", str(good), "--out", str(tmp_path / "o1"), "--report", str(report)]) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["succeeded"] == 1
    assert main.main(["--cli", str(good), str(bad), "--out", str(tmp_path / "o2")]) == 4
    assert main.main(["--cli", str(bad), "--out", str(tmp_path / "o3")]) == 1
    assert main.main(["--cli", str(tmp_path / "nothing_here.pdf")]) == 5
    # v1.0 single-file syntax still works, --ocr reaches the engine
    assert main.main(["--cli", str(good), "--ocr", "--out", str(tmp_path / "o4")]) == 0
    assert fake_engine.calls()[-1]["ocr"] is True
    for removed in (["--overwrite"], ["--collision", "skip"], ["--retries", "2"], ["--timeout", "5"]):
        assert main.main(["--cli", str(good), *removed]) == 64
    with pytest.raises(SystemExit) as exc:
        main.main(["--cli", str(good), "--ocr-mode", "auto"])
    assert exc.value.code == 64
    monkeypatch.setattr(dr, "detect_docling", lambda *a, **k: dr.DoclingInfo(False, error="not found"))
    assert main.main(["--cli", str(good)]) == 3


def test_v2_settings_migrate_to_ocr_off(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"ocr_mode": "auto", "ocr_enabled": False, "collision_policy": "replace",
                             "timeout_minutes": 60}), encoding="utf-8")
    assert Settings.load(p).ocr_enabled is False
    p.write_text(json.dumps({"ocr_mode": "on", "ocr_enabled": False}), encoding="utf-8")
    assert Settings.load(p).ocr_enabled is True
    p.write_text(json.dumps({"ocr_enabled": True}), encoding="utf-8")  # v1.0
    assert Settings.load(p).ocr_enabled is True
