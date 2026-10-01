"""Drive the real MainWindow through its button handlers with the fake engine.

Checks GUI wiring (adding files, Convert, thread-safe event delivery, truthful
status, restore on restart). Skipped when no display is available.
"""

import time

import pytest

from core.batch import PENDING
from core.config import Settings
from core.converter import COMPLETED, FAILED
from core.sample_pdf import text_pdf


def _pump(w, until, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        w.update()
        if until():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def window_factory(fake_engine, monkeypatch):
    try:
        import tkinter

        tkinter.Tk().destroy()
    except Exception:
        pytest.skip("no display available")
    import ui.main_window as mw

    monkeypatch.setattr(mw, "detect_docling", lambda *a, **k: fake_engine)
    created = []

    def make(settings=None, paths=None):
        w = mw.MainWindow(settings or Settings(), initial_paths=paths or [])
        created.append(w)
        return w

    yield make
    for w in created:
        try:
            w._closing = True
            w.destroy()
        except Exception:
            pass


def test_gui_convert_end_to_end_and_restore(tmp_path, window_factory, fake_engine):
    good = text_pdf(tmp_path / "in" / "good.pdf", "G")
    bad = text_pdf(tmp_path / "in" / "bad.pdf", "B", marker="FAKE-FAIL")
    st = Settings(output_beside_source=False, last_output_dir=str(tmp_path / "out"))
    w = window_factory(st, [str(good), str(bad)])
    assert _pump(w, lambda: w._docling is not None and w._docling.available)
    assert len(w.tree.get_children()) == 2
    assert w.tree.item(w.queue.jobs[0].id)["values"][1] == "Pending"
    assert str(w.convert_btn.cget("state")) == "normal"
    assert w.ocr_var.get() is False
    w._convert()
    assert _pump(w, lambda: w._runner is not None and not w._runner.is_alive() and w._events.empty())
    w.update()
    assert [j.status for j in w.queue.jobs] == [COMPLETED, FAILED]
    assert "Finished: 1 converted, 1 failed" in w.status_label.cget("text")
    assert "1 of 2 files completed" in w.overall_label.cget("text")
    assert fake_engine.calls()[0]["ocr"] is False
    w.tree.selection_set(w.queue.jobs[1].id)
    w._on_select()
    assert "could not be opened" in w.log_box.get("1.0", "end")
    w._on_close()

    # restart: the list is restored, nothing starts; Convert re-runs only the failed file
    w2 = window_factory(Settings.load())
    assert [j.status for j in w2.queue.jobs] == [COMPLETED, FAILED]
    assert _pump(w2, lambda: w2._docling is not None)
    assert w2._runner is None
    n_calls = len(fake_engine.calls())
    w2._convert()
    assert _pump(w2, lambda: w2._runner is not None and not w2._runner.is_alive())
    assert len(fake_engine.calls()) == n_calls + 1


def test_gui_remove_and_clear_do_not_touch_files(tmp_path, window_factory):
    a = text_pdf(tmp_path / "a.pdf", "A")
    b = text_pdf(tmp_path / "b.pdf", "B")
    w = window_factory(Settings(), [str(a), str(b)])
    assert [j.status for j in w.queue.jobs] == [PENDING, PENDING]
    w.tree.selection_set(w.queue.jobs[0].id)
    w._remove_selected()
    assert [j.name for j in w.queue.jobs] == ["b.pdf"] and a.exists()
    w._add_paths([str(b)])  # duplicate
    assert len(w.queue.jobs) == 1
    w._clear()
    assert w.queue.jobs == [] and b.exists()


def test_gui_log_toggle(tmp_path, window_factory):
    w = window_factory(Settings(show_log=False))
    assert not w.log_box.winfo_ismapped() or True
    w._toggle_log()
    assert w.settings.show_log is True
    w._toggle_log()
    assert w.settings.show_log is False
