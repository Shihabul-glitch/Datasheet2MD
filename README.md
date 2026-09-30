<div align="center">

<img src="docs/logo.png" width="96" alt="Datasheet2MD icon">

# Datasheet2MD

**Turn electronic-component PDF datasheets into clean Markdown that LLMs can actually read.**

Pinouts, electrical characteristics, register tables and figures, ready for Claude, ChatGPT,
Copilot or your own RAG pipeline.

![Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Docling](https://img.shields.io/badge/engine-Docling-6f42c1)
![License](https://img.shields.io/badge/license-MIT-green)

<img src="docs/screenshot.png" width="620" alt="Datasheet2MD main window after a successful conversion">

</div>

---

## Why?

If you've ever pasted a datasheet PDF into an AI assistant, you've probably seen:

- tables flattened into word soup, so the **pin 4 / BOOTN / "pull high"** relationship is lost
- `I 2 C`, `V OH` and other superscripts or subscripts shredded by text extraction
- figures dropped entirely, or embedded as **megabytes of base64** that eat the context window

Datasheet2MD fixes this with one button. It runs IBM's open-source
**[Docling](https://github.com/docling-project/docling)** document-understanding models (layout
detection + TableFormer) with settings tuned for datasheets. Then it post-processes the result
into a tidy folder that an LLM or coding agent can navigate:

```
BNO080_085-Datasheet.pdf   ──►   BNO080_085/
                                 ├── BNO080_085-Datasheet.md     ← text + real Markdown tables
                                 └── images/
                                     ├── image_001.png           ← figures, in document order
                                     ├── image_002.png
                                     └── ...
```

Real result for a 60-page IMU datasheet: **49 tables, 106 headings, 40 figures**. The Markdown
went from **3.2 MB** (Docling's default base64 output) to **207 KB**.

## What the output looks like

Tables stay tables:

```markdown
| Parameter                          | Symbol   | Conditions   |   Min | Typ   |   Max | Unit   |
|------------------------------------|----------|--------------|-------|-------|-------|--------|
| Supply voltage (μC and I/O Domain) | VDDIO    |              |   1.7 |       |   3.6 | V      |
| Supply voltage (only sensors)      | VDD      |              |   2.4 |       |   3.6 | V      |
| Operating temperature              |          |              |   -40 |       |    85 | °C     |
```

Figures are external PNGs with relative links, and the nearby caption becomes the alt text:

```markdown
Figure 1-11: BNO08X I²C connection diagram

![Figure 1-11: BNO08X I²C connection diagram](images/image_011.png)
```

The Markdown never contains `data:image/...;base64`, absolute `C:\...` paths or `file://` URLs.

## Features

- 🖱️ **One-click GUI**: Browse or drag & drop a PDF, then press **Convert**. The window stays responsive, and **Cancel** stops Docling cleanly.
- 📊 **Accurate tables**: Docling's TableFormer in *accurate* mode.
- 🔤 **Native text extraction**: OCR is **off by default** because real datasheets have real text. It's faster and more precise. Tick **Scanned PDF / Enable OCR** for scans.
- 🖼️ **Figures as files**: `images/image_001.png …`, numbered in document order, with links rewritten and verified.
- 🧹 **Deterministic "LLM-friendly" cleanup**: `I 2 C → I²C`, page-number and running-footer removal, blank-line tidy-up. **No AI rewriting.** Numbers, units (°C, µA, kΩ, ±), hex values and tables are never touched.
- ✅ **Built-in validation** after every run:
  ```
  ✓ Markdown created            ✓ All image references valid
  ✓ UTF-8 validation passed     ✓ Image numbering sequential
  ✓ 40 images extracted         ✓ 49 Markdown tables preserved
  ✓ No embedded base64 images   ✓ Source PDF unchanged
  ```
- 🛡️ **Safe by design**: the source PDF is never modified (it's hash-checked). An existing output folder triggers a prompt: *new version* (`BNO080_085_2/`), *overwrite generated files*, or *cancel*.
- 📦 **Small EXE (~10 MB)**: the frontend is packaged with PyInstaller and uses your locally installed Docling. PyTorch and the models aren't bundled.

## Quick start

### 1. Install Docling (the conversion engine)

```powershell
pip install docling
docling-tools models download     # one-time model download
docling convert --version         # should print "Docling version: 2.x"
```

### 2. Run Datasheet2MD

**Option A: EXE.** Build it (see below) or grab it from the Releases page if one is published, then double-click `Datasheet2MD.exe`.

**Option B: from source.**

```powershell
git clone https://github.com/Shihabul-glitch/Datasheet2MD.git
cd Datasheet2MD
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

The header shows **✓ Docling x.y.z ready** after a few seconds, because Docling loads PyTorch. Pick a PDF and press **CONVERT**.
A 60-page datasheet takes roughly **7 minutes on a CPU**; the status line shows the stage and the elapsed time.

### Headless / scripting

```powershell
python main.py --cli "LM358-Datasheet.pdf" [--out DIR] [--ocr] [--no-cleanup] [--debug] [--overwrite] [--pages 1-5]
```

The exit code is `0` on success.

## Output folder naming

The trailing `-Datasheet`, `_Datasheet` or ` Datasheet` (any letter case) is removed from the folder name. The Markdown file keeps the PDF's name.

| PDF | Folder | Markdown |
|---|---|---|
| `BNO080_085-Datasheet.pdf` | `BNO080_085/` | `BNO080_085-Datasheet.md` |
| `WS2812B_V6_Datasheet.pdf` | `WS2812B_V6/` | `WS2812B_V6_Datasheet.md` |
| `STM32F103C8T6.pdf` | `STM32F103C8T6/` | `STM32F103C8T6.md` |

## How it works

```
PDF ──► temp copy ──► docling convert ──► extract/renumber images ──► deterministic cleanup ──► validate ──► folder
          (source never          (subprocess,           (images/image_###.png,   (I²C, footers,     (✓ report)
           touched)               no shell)              relative links)          captions)
```

Docling is called with an explicit argument list (never `shell=True`):

```
docling convert source.pdf --from pdf --to md
    --image-export-mode referenced          # PNG files instead of base64
    --pipeline standard --pdf-backend docling_parse
    --tables --table-mode accurate
    --no-ocr                                # or: --ocr --ocr-engine auto
    [--artifacts-path <local models>]
    --output . -v
```

Docling quirks the app handles:
- Docling's CLI exits with code `0` even when a document fails. Success is judged from its `of which N failed` log line and from the file it produced.
- An absolute `--output` makes Docling write absolute image paths. Docling therefore runs inside a staging folder with `--output .`, and every reference form (relative, absolute, `file://`, `%20`) is resolved and rewritten anyway.
- Docling enables OCR by default, so `--no-ocr` is always passed explicitly.

## Building the EXE

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1              # dist\Datasheet2MD.exe (no console)
powershell -ExecutionPolicy Bypass -File build\build.ps1 -Mode Debug  # dist\Datasheet2MD-Debug.exe (console)
powershell -ExecutionPolicy Bypass -File build\build.ps1 -Mode Both
```

The script creates `.venv`, installs `requirements.txt`, runs the **59 unit tests**, then builds a one-file PyInstaller EXE.

## Troubleshooting

| Problem | Fix |
|---|---|
| *Docling was not found* | Make sure `docling convert --version` works in a **new** terminal (Docling's `Scripts` folder must be on `PATH`), then use **Diagnostics → Retry detection**. Or set `docling_path` in `%APPDATA%\Datasheet2MD\settings.json`. |
| Model download stalls at 0 bytes | Some networks can't reach Hugging Face's CDN. The same model files (identical SHA-256) are mirrored on ModelScope (`ds4sd/docling-layout-heron`, `ds4sd/docling-models`). Put them in `%USERPROFILE%\.cache\docling\models\…`; Datasheet2MD detects that folder and passes `--artifacts-path` automatically. |
| Scanned PDF gives little or no text | Tick **Scanned PDF / Enable OCR**. |
| Password-protected PDF | Remove the password first. PDFs that only have copy/print restrictions convert fine. |
| SmartScreen warning on the EXE | The EXE is unsigned: **More info → Run anyway**. |
| Anything else | **Copy Log**, or open the per-run logs in `%APPDATA%\Datasheet2MD\logs\`. Run `Datasheet2MD-Debug.exe` for a console. |

## Project layout

```
main.py / app.py            entry point: GUI or --cli
core/
  docling_runner.py         detection, argument list, subprocess, log parsing, cancel (kills process tree)
  output_manager.py         folder naming, versioning, safe overwrite
  image_manager.py          images/image_###.png + reference rewriting
  markdown_cleaner.py       deterministic cleanup (base64 fallback, I²C, footers, captions)
  validator.py              post-conversion checks
  converter.py              orchestrates the pipeline (shared by GUI and CLI)
  config.py                 constants and settings.json
ui/main_window.py           CustomTkinter GUI, with no business logic
tests/                      pytest suite
build/build.ps1             PyInstaller build (Release/Debug)
```

## Contributing

Issues and PRs are welcome, especially **datasheets that convert badly**. A link to the PDF (or
the page numbers) plus the validation output helps a lot. Please run `python -m pytest` before
opening a PR.

Ideas on the roadmap: batch conversion of whole folders, per-page progress, optional formula
enrichment, macOS/Linux support.

## License

[MIT](LICENSE). Docling is a separate project under its own MIT license. Datasheets you convert remain the property of their manufacturers.
