from pathlib import Path

import pytest

from core.output_manager import (
    ExistsPolicy,
    OutputError,
    component_folder_name,
    discard_output,
    markdown_filename,
    next_version_dir,
    plan_output,
    prepare_output,
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
    ],
)
def test_names(pdf, folder, md):
    assert component_folder_name(pdf) == folder
    assert markdown_filename(pdf) == md


def test_name_from_full_path():
    assert component_folder_name(r"C:\some dir\BNO080_085-Datasheet.pdf") == "BNO080_085"


def test_next_version_dir(tmp_path):
    base = tmp_path / "BNO080_085"
    assert next_version_dir(base) == base
    base.mkdir()
    assert next_version_dir(base).name == "BNO080_085_2"
    (tmp_path / "BNO080_085_2").mkdir()
    assert next_version_dir(base).name == "BNO080_085_3"


def _pdf(tmp_path, name="BNO080_085-Datasheet.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.7\n")
    return p


def test_plan_defaults_to_pdf_directory(tmp_path):
    plan = plan_output(_pdf(tmp_path))
    assert plan.component_dir == tmp_path.resolve() / "BNO080_085"
    assert plan.markdown_path.name == "BNO080_085-Datasheet.md"
    assert plan.images_dir == plan.component_dir / "images"
    assert not plan.existed_before


def test_plan_custom_output_root(tmp_path):
    out = tmp_path / "out dir"
    plan = plan_output(_pdf(tmp_path), out)
    assert plan.component_dir == out.resolve() / "BNO080_085"


def test_new_version_policy(tmp_path):
    pdf = _pdf(tmp_path)
    (tmp_path / "BNO080_085").mkdir()
    plan = plan_output(pdf, policy=ExistsPolicy.NEW_VERSION)
    assert plan.component_dir.name == "BNO080_085_2"


def test_cancel_policy(tmp_path):
    with pytest.raises(OutputError):
        plan_output(_pdf(tmp_path), policy=ExistsPolicy.CANCEL)


def test_overwrite_removes_only_generated_files(tmp_path):
    pdf = _pdf(tmp_path)
    comp = tmp_path / "BNO080_085"
    (comp / "images").mkdir(parents=True)
    (comp / "images" / "image_001.png").write_bytes(b"x")
    (comp / "BNO080_085-Datasheet.md").write_text("old")
    (comp / "my_notes.txt").write_text("keep me")
    plan = plan_output(pdf, policy=ExistsPolicy.OVERWRITE)
    assert plan.component_dir == comp.resolve() and plan.existed_before
    prepare_output(plan)
    assert not plan.markdown_path.exists()
    assert not (comp / "images").exists()
    assert (comp / "my_notes.txt").read_text() == "keep me"
    assert plan.staging_dir.is_dir()
    assert pdf.read_bytes() == b"%PDF-1.7\n"


def test_overwrite_never_deletes_source_pdf_inside_images(tmp_path):
    comp = tmp_path / "X"
    (comp / "images").mkdir(parents=True)
    pdf = comp / "images" / "X-Datasheet.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    plan = plan_output(pdf, tmp_path, ExistsPolicy.OVERWRITE)
    with pytest.raises(OutputError):
        prepare_output(plan)
    assert pdf.exists()


def test_discard_removes_new_folder_but_not_existing(tmp_path):
    pdf = _pdf(tmp_path)
    plan = plan_output(pdf)
    prepare_output(plan)
    discard_output(plan)
    assert not plan.component_dir.exists()
    assert pdf.exists()

    comp = tmp_path / "BNO080_085"
    comp.mkdir()
    (comp / "keep.txt").write_text("k")
    plan2 = plan_output(pdf, policy=ExistsPolicy.OVERWRITE)
    prepare_output(plan2)
    discard_output(plan2)
    assert (comp / "keep.txt").exists()
    assert not plan2.staging_dir.exists()
