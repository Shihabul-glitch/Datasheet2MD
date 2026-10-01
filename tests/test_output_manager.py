import os

import pytest

from core.output_manager import (
    OutputError,
    Reservations,
    check_path_lengths,
    component_folder_name,
    create_staging,
    markdown_filename,
    plan_destination,
    publish_new,
    remove_staging,
    sanitize_name,
)


@pytest.mark.parametrize(
    "pdf, folder, md",
    [
        ("BNO080_085-Datasheet.pdf", "BNO080_085", "BNO080_085-Datasheet.md"),
        ("BME688-Datasheet.pdf", "BME688", "BME688-Datasheet.md"),
        ("WS2812B_V6_Datasheet.pdf", "WS2812B_V6", "WS2812B_V6_Datasheet.md"),
        ("STM32F103C8T6.pdf", "STM32F103C8T6", "STM32F103C8T6.md"),
        ("LM358 Datasheet.pdf", "LM358", "LM358 Datasheet.md"),
        ("ads1115-datasheet.pdf", "ads1115", "ads1115-datasheet.md"),
        ("ads1115_datasheet.pdf", "ads1115", "ads1115_datasheet.md"),
        ("NE555 datasheet.pdf", "NE555", "NE555 datasheet.md"),
        ("Datasheet.pdf", "Datasheet", "Datasheet.md"),
        ("TPS63020 (rev B).pdf", "TPS63020 (rev B)", "TPS63020 (rev B).md"),
        ("µ-Sensor & Co_Datasheet.pdf", "µ-Sensor & Co", "µ-Sensor & Co_Datasheet.md"),
        ("MyDatasheet.pdf", "MyDatasheet", "MyDatasheet.md"),  # no separator: not a suffix
        ("传感器 BMI270-datasheet.pdf", "传感器 BMI270", "传感器 BMI270-datasheet.md"),
    ],
)
def test_names(pdf, folder, md):
    assert component_folder_name(pdf) == folder
    assert markdown_filename(pdf) == md


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CON", "CON_"), ("nul", "nul_"), ("COM1.backup", "COM1.backup_"), ("LPT9", "LPT9_"),
        ("name. . ", "name"), ("  spaced  ", "spaced"), ('a<b>c:d"e|f?g*h', "a_b_c_d_e_f_g_h"),
        ("tab\there", "tab_here"), ("...", "datasheet"), ("", "datasheet"), ("CONSOLE", "CONSOLE"),
    ],
)
def test_sanitize_name(raw, expected):
    assert sanitize_name(raw) == expected


def test_reserved_and_trailing_dot_pdf_names():
    assert component_folder_name("CON.pdf") == "CON_"
    assert component_folder_name("AUX-Datasheet.pdf") == "AUX_"
    assert markdown_filename("NUL.pdf") == "NUL_.md"
    assert component_folder_name("chip..pdf") == "chip"
    assert len(component_folder_name("x" * 300 + ".pdf")) <= 120


def test_name_from_full_path():
    assert component_folder_name(r"C:\some dir\BNO080_085-Datasheet.pdf") == "BNO080_085"


def _pdf(folder, name="BNO080_085-Datasheet.pdf"):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"%PDF-1.7\n")
    return p


def test_plan_beside_pdf_and_custom_root(tmp_path):
    pdf = _pdf(tmp_path)
    plan = plan_destination(pdf, None)
    assert plan.target_dir == tmp_path.resolve() / "BNO080_085"
    assert plan.markdown_path.name == "BNO080_085-Datasheet.md"
    out = tmp_path / "out dir"
    assert plan_destination(pdf, out).target_dir == out.resolve() / "BNO080_085"


def test_duplicate_stems_from_different_folders_get_distinct_reserved_folders(tmp_path):
    a = _pdf(tmp_path / "a", "LM358.pdf")
    b = _pdf(tmp_path / "b", "LM358.pdf")
    out = tmp_path / "out"
    res = Reservations()
    pa = plan_destination(a, out, res, "job-a")
    pb = plan_destination(b, out, res, "job-b")
    assert pa.target_dir.name == "LM358" and pb.target_dir.name == "LM358_2"
    # re-planning the same job keeps its own reservation
    assert plan_destination(a, out, res, "job-a").target_dir.name == "LM358"
    # an existing folder (e.g. a previous run) is never reused
    (out / "LM358_3").mkdir(parents=True)
    pc = plan_destination(_pdf(tmp_path / "c", "LM358.pdf"), out, res, "job-c")
    assert pc.target_dir.name == "LM358_4" and pc.notes


def _staged(tmp_path, files, name="stage"):
    stage = tmp_path / name
    for rel, content in files.items():
        p = stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return stage


def test_publish_new_is_atomic_rename_and_never_merges(tmp_path):
    pdf = _pdf(tmp_path / "src")
    out = tmp_path / "out"
    plan = plan_destination(pdf, out)
    # someone creates the folder after planning: publish must pick another name, not merge
    plan.target_dir.mkdir(parents=True)
    (plan.target_dir / "user.txt").write_text("mine")
    stage = _staged(tmp_path, {"a.md": b"x"})
    final = publish_new(stage, plan)
    assert final.name == "BNO080_085_2" and (final / "a.md").exists()
    assert sorted(os.listdir(plan.target_dir)) == ["user.txt"]


def test_staging_is_unique_and_removed(tmp_path):
    a = create_staging(tmp_path, "job1")
    b = create_staging(tmp_path, "job1")
    assert a != b and a.parent == b.parent
    remove_staging(a)
    assert not a.exists() and b.exists()
    remove_staging(b)
    assert not b.parent.exists()


def test_remove_staging_refuses_foreign_folder(tmp_path):
    victim = tmp_path / "important"
    victim.mkdir()
    remove_staging(victim)
    assert victim.exists()


def test_path_length_check(tmp_path, monkeypatch):
    import core.output_manager as om

    monkeypatch.setattr(om, "long_paths_enabled", lambda: False)
    with pytest.raises(OutputError, match="Windows limit"):
        check_path_lengths(tmp_path / ("d" * 230), "x.md")
    check_path_lengths(tmp_path / "short", "x.md")

