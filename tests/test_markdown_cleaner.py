import base64

from core.markdown_cleaner import (
    add_caption_alt_text,
    clean_markdown,
    extract_embedded_images,
    normalize_i2c,
    normalize_whitespace,
    remove_page_artifacts,
)

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


# ------------------------------------------------------------------ base64
def test_extract_markdown_base64(tmp_path):
    md = f"Intro\n\n![Image](data:image/png;base64,{PNG_B64})\n\nEnd\n"
    out, n, warnings = extract_embedded_images(md, tmp_path / "embedded", "embedded")
    assert n == 1 and warnings
    assert "data:image" not in out
    assert "![Image](embedded/embedded_0001.png)" in out
    assert (tmp_path / "embedded" / "embedded_0001.png").read_bytes() == base64.b64decode(PNG_B64)


def test_extract_html_and_jpeg(tmp_path):
    md = f'<img src="data:image/jpeg;base64,{PNG_B64}" alt="Pinout">\n'
    out, n, _ = extract_embedded_images(md, tmp_path, "e")
    assert n == 1 and out == "![Pinout](e/embedded_0001.jpg)\n"


def test_undecodable_base64_is_removed_not_left(tmp_path):
    md = "![x](data:image/png;base64,)\n"
    out, n, warnings = extract_embedded_images(md, tmp_path, "e")
    assert n == 0 and "data:image" not in out and warnings


def test_no_data_uri_is_noop(tmp_path):
    md = "![x](images/image_001.png)\n"
    assert extract_embedded_images(md, tmp_path, "e") == (md, 0, [])


# ------------------------------------------------------------------ I2C
def test_i2c_variants():
    md = "## 1.2.2 I 2 C interface\n\nThe I 2 C bus, I2 C and I 2C. (I 2 C)\n"
    out, n = normalize_i2c(md)
    assert n == 5
    assert out == "## 1.2.2 I²C interface\n\nThe I²C bus, I²C and I²C. (I²C)\n"


def test_i2c_does_not_touch_identifiers_or_numbers():
    md = "I2C_ADDR = 0x4A, HI 2 C3, I2C, 2 C, I 2 CS, AI 2 C, x/I 2 C, 12 C\n| I 2 C | 400 kHz |\n"
    out, n = normalize_i2c(md)
    assert n == 1
    assert out == "I2C_ADDR = 0x4A, HI 2 C3, I2C, 2 C, I 2 CS, AI 2 C, x/I 2 C, 12 C\n| I²C | 400 kHz |\n"


def test_i2c_not_in_code_fence():
    md = "```\nI 2 C\n```\nI 2 C\n"
    out, n = normalize_i2c(md)
    assert out == "```\nI 2 C\n```\nI²C\n" and n == 1


# ------------------------------------------------------------------ page artifacts
def test_page_numbers_removed():
    md = "Text\n\nPage 3 of 40\n\n- 4 -\n\n12 / 40\n\nMore\n"
    out, n = remove_page_artifacts(md)
    assert n == 3 and "Page 3" not in out and "- 4 -" not in out and "12 / 40" not in out
    assert "Text" in out and "More" in out


def test_repeated_footer_keeps_first_occurrence():
    footer = "© 2023 ACME Corp. All rights reserved"
    md = "\n\n".join(["A", footer, "B", footer, "C", footer, "D"])
    out, n = remove_page_artifacts(md)
    assert n == 2 and out.count(footer) == 1


def test_repeated_technical_notes_are_kept():
    note = "3. BOOTN is sampled at reset. If low the BNO08X will enter bootloader mode."
    md = "\n\n".join([note, "x", note, "y", note])
    out, n = remove_page_artifacts(md)
    assert n == 0 and out == md


def test_repeated_non_footer_short_lines_kept():
    md = "\n\n".join(["VDD", "a", "VDD", "b", "VDD", "c"])
    assert remove_page_artifacts(md) == (md, 0)


def test_table_rows_never_removed():
    md = "| Page 3 of 4 |\n|---|\n| 1 / 2 |\n"
    assert remove_page_artifacts(md) == (md, 0)


# ------------------------------------------------------------------ captions
def test_caption_before_image():
    md = "Figure 1-1: BNO08X block diagram\n\n![Image](images/image_001.png)\n"
    out, n = add_caption_alt_text(md)
    assert n == 1
    assert "![Figure 1-1: BNO08X block diagram](images/image_001.png)" in out
    assert out.startswith("Figure 1-1: BNO08X block diagram\n")  # caption text kept


def test_caption_after_image_only_if_not_followed_by_image():
    md = "![Image](images/image_001.png)\n\nFigure 2: Timing\n\n![Image](images/image_002.png)\n"
    out, n = add_caption_alt_text(md)
    assert n == 1
    assert "![Image](images/image_001.png)" in out
    assert "![Figure 2: Timing](images/image_002.png)" in out


def test_caption_brackets_escaped_and_specific_alt_kept():
    md = "Figure 3: Pin [1] map\n\n![Image](a.png)\n\nFigure 4: X\n\n![Logo](b.png)\n"
    out, _ = add_caption_alt_text(md)
    assert "![Figure 3: Pin (1) map](a.png)" in out and "![Logo](b.png)" in out


# ------------------------------------------------------------------ whitespace / full
def test_whitespace():
    md = "\n\nA  \n\n\n\nB\n```\ncode  \n\n\n\n```\n\n\n"
    out, removed = normalize_whitespace(md)
    assert out == "A\n\nB\n```\ncode  \n\n\n\n```"
    assert removed > 0


def test_technical_values_preserved():
    md = (
        "## 6.2 Recommended Operating Conditions\n\n"
        "| Parameter | Symbol | Min | Typ | Max | Unit |\n"
        "|---|---|---|---|---|---|\n"
        "| Supply voltage | VDD | 2.4 | | 3.6 | V |\n"
        "| Operating temperature | | -40 | | 85 | °C |\n"
        "| Current | IDD | | 12.5 | | µA |\n"
        "| Pull-up | R | | 4.7 | | kΩ |\n"
        "| Clock | f | | 32.768 | | kHz |\n"
        "| Pipe &#124; escaped | | ±2 | | 0x7F / 0b1010 | dB |\n\n"
        "0.9 * RequestedRate &lt;= ConfiguredRate &lt;= 2.1 * RequestedRate\n\n"
        "V<sub>OH</sub> = 0.8×V<sub>DDIO</sub>, 10<sup>-3</sup> MHz, H_INTN, 2³²\n"
    )
    out, report = clean_markdown(md, llm_friendly=True)
    assert out == md  # nothing to change


def test_llm_cleanup_disabled_only_normalizes_line_endings():
    md = "﻿I 2 C\r\n\r\n\r\n\r\nx  \r\n"
    out, report = clean_markdown(md, llm_friendly=False)
    assert out == "I 2 C\n\n\n\nx  \n"
    assert report.i2c_fixed == 0


def test_full_clean_output_is_str_with_single_trailing_newline():
    out, report = clean_markdown("I 2 C\n\n\n\nPage 2\n\nx", True)
    assert out == "I²C\n\nx\n"
    assert report.i2c_fixed == 1 and report.page_artifacts_removed == 1
