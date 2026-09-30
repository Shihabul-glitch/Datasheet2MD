import base64
from pathlib import Path

from core.image_manager import iter_image_refs, local_image_targets, normalize_images, resolve_local_target

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _img(path: Path, payload: bytes = PNG) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_numbering_follows_document_order(tmp_path):
    base = tmp_path / "work"
    _img(base / "source_artifacts" / "image_000001_bbb.png", PNG + b"b")
    _img(base / "source_artifacts" / "image_000000_aaa.png", PNG + b"a")
    md = (
        "# T\n\n![Image](source_artifacts/image_000001_bbb.png)\n\ntext\n\n"
        "![Image](source_artifacts/image_000000_aaa.png)\n"
    )
    res = normalize_images(md, base, tmp_path / "out" / "images")
    assert res.count == 2
    assert "![Image](images/image_001.png)" in res.markdown
    assert res.markdown.index("image_001") < res.markdown.index("image_002")
    # image_001 must be the file that appeared first (the "b" one)
    assert (tmp_path / "out" / "images" / "image_001.png").read_bytes().endswith(b"b")
    assert "source_artifacts" not in res.markdown


def test_absolute_file_uri_and_percent_encoded(tmp_path):
    base = tmp_path / "work"
    a = _img(tmp_path / "abs dir" / "a.png")
    b = _img(base / "my images" / "b.png")
    c = _img(tmp_path / "uri dir" / "c.png")
    md = (
        f"![A]({a})\n\n"
        "![B](my%20images/b.png)\n\n"
        f"![C]({c.as_uri()})\n\n"
        f"![B again](<my images/b.png>)\n"
    )
    res = normalize_images(md, base, tmp_path / "images")
    assert res.count == 3 and res.duplicates == 1
    assert local_image_targets(res.markdown) == [
        "images/image_001.png", "images/image_002.png", "images/image_003.png", "images/image_002.png"
    ]
    assert "\\" not in res.markdown and "file:" not in res.markdown and str(tmp_path) not in res.markdown


def test_missing_image_becomes_placeholder_not_broken_link(tmp_path):
    md = "![Block diagram](source_artifacts/nope.png)\n"
    res = normalize_images(md, tmp_path, tmp_path / "images")
    assert res.missing == ["source_artifacts/nope.png"]
    assert "![" not in res.markdown
    assert "*[Image missing: Block diagram]*" in res.markdown


def test_remote_and_data_uris_untouched(tmp_path):
    md = "![r](https://example.com/x.png)\n![d](data:image/png;base64,AAAA)\n"
    res = normalize_images(md, tmp_path, tmp_path / "images")
    assert res.markdown == md and res.count == 0 and res.remote == ["https://example.com/x.png"]
    assert not (tmp_path / "images").exists()


def test_code_fence_is_ignored(tmp_path):
    _img(tmp_path / "a.png")
    md = "```\n![x](a.png)\n```\n\n![y](a.png)\n"
    res = normalize_images(md, tmp_path, tmp_path / "images")
    assert "```\n![x](a.png)\n```" in res.markdown
    assert "![y](images/image_001.png)" in res.markdown


def test_title_preserved_and_parentheses_in_name(tmp_path):
    _img(tmp_path / "fig(1).png")
    md = '![Pinout](fig(1).png "Pin diagram")\n'
    res = normalize_images(md, tmp_path, tmp_path / "images")
    assert res.markdown == '![Pinout](images/image_001.png "Pin diagram")\n'


def test_width_grows_beyond_999(tmp_path):
    lines = []
    for i in range(1000):
        _img(tmp_path / "src" / f"{i}.png", PNG + str(i).encode())
        lines.append(f"![Image](src/{i}.png)")
    res = normalize_images("\n\n".join(lines), tmp_path, tmp_path / "images")
    assert res.image_files[0].name == "image_0001.png"
    assert res.image_files[-1].name == "image_1000.png"


def test_html_img_is_normalized(tmp_path):
    _img(tmp_path / "a.png")
    res = normalize_images('<img src="a.png" alt="Pin [1]">\n', tmp_path, tmp_path / "images")
    assert res.markdown == "![Pin \\[1\\]](images/image_001.png)\n"


def test_resolve_windows_absolute(tmp_path):
    p = _img(tmp_path / "x.png")
    assert resolve_local_target(str(p), Path("C:/nonexistent")) == p.resolve()
    assert len(list(iter_image_refs("![a](x.png) ![b](y.png)"))) == 2
