# Datasheet2MD 2.1: simplification and performance report

**Release:** Datasheet2MD 2.1.0 replaces the V2 (2.0) working tree. V1 is the committed
`c73abe8`. V2 was never committed; it is archived in `.v2_backup/datasheet2md_v2_working_tree.tar.gz`
(gitignored).

**Machine:**
- Intel i5-8250U (4 cores / 8 threads), 15.9 GB RAM, NVIDIA GeForce MX110 (2 GB, compute 5.0)
- Windows 10 Pro 19045
- Docling 2.131.0 with docling-core 2.99.0, docling-parse 7.22.1 and PyTorch **2.14.0+cpu**
- RapidOCR 3.9.2, torch backend (no onnxruntime, EasyOCR or Tesseract installed)

## 1. Confirmed causes of the slowdown

| # | Finding | Evidence |
|---|---|---|
| 1 | **Auto OCR switched OCR on for normal datasheets.** In WS2812B (18 pages), V2's page analysis flagged pages 9 and 13 as "scanned" because a large image covered them with little text. OCR was then enabled for the whole document. Docling's region-aware OCR runs RapidOCR (torch, CPU) on *every* layout region containing a bitmap or vector shape. That covers figures and ruled tables on all pages, not just the two flagged ones. | Your log: 1044.6 s for this file. Same file, same code, only OCR changed: V2 Auto **1030.9 s** (986.8 s Docling processing) vs V2 OCR-off **121.2 s** (82.9 s). |
| 2 | **OCR models add memory.** | Peak resident memory of the app's process tree: 2427 MB with OCR vs 1835 MB without (same file). |
| 3 | **Misleading "Loading OCR models" label.** V2 derived the stage from log text: any line containing "RapidOCR" set the stage to "Loading OCR models". RapidOCR's repeated `returned empty result!` warnings during page processing kept resetting it. So the label was stale, not a real stage. | V2 `docling_runner._STAGE_PATTERNS`; your log has 5 such warnings between 10:46 and 10:55. |
| 4 | **Every PDF paid full engine start-up.** Each file started a new Python process, imported Docling and PyTorch, and loaded the models. This is not a V2 regression (V1 does the same), but it is the largest fixed cost per file. | 24–50 s from launch to "Processing document" per file in V1 and V2 runs. Measured parts: `import docling.cli.main` 9.9 s (warm disk cache), model loading 5.5–11.3 s. |

**Not causes** (measured, so not merely assumed):
- **V2 with OCR off is within run-to-run noise of V1:** 121.2 vs 97.9 s on WS2812B, and 225.1 vs 257.1 s on 74AHCT, where V2 was faster.
- **V2's extra outputs were small costs:**
  - JSON export: 3.6–6 s per file in your logs.
  - Merged-table HTML and the per-document report: under 1 s.
  - pypdfium2 page analysis: 0.6–1.0 s.
- **No duplicate conversion passes.** V2 converted each PDF once. Its retries only triggered on transient failures, and none occurred in the measured runs.
- **The "80 % RAM" figure is mostly other programs.** Before any conversion started, system memory was already at 68–72 %. The conversion itself adds 1.8–2.7 GB depending on the document (V1 has the same peaks), taking the system to 79–86 %.

**Suspicion, not confirmed:** CPU thermal throttling or background load. The same 14-page 74AHCT
conversion took between 142 s and 257 s across runs with identical Docling settings. So small
timing differences between versions aren't meaningful, and nothing below claims precision beyond
that noise.

## 2. What was removed or disabled

**Normal workflow:**
- Auto OCR. OCR is **off** unless *Scanned PDF / Enable OCR* is ticked.
- Force full-page OCR.
- The pypdfium2 page analysis (and the pypdfium2 dependency).

**Document output folders:**
- `conversion_report.json`
- `.datasheet2md-manifest.json`
- `tables/*.html` merged-cell side files
- Docling JSON export (Docling now writes Markdown only)
- `_docling.json` debug copy

**Behaviour:**
- Collision policies *Skip* and *Replace app-generated files*. Existing output is never touched;
  a new `NAME_2` folder is created.
- Pause/Resume, Retry Failed (Convert now converts everything not completed), per-file timeouts,
  automatic retries, and the GUI batch JSON/CSV export.

**CLI:**
- Rejected with exit 64 and an explanation: `--overwrite`, `--collision`, `--timeout`, `--retries`,
  `--report-csv`, `--continue-on-error` (now the default), `--ocr-mode auto|force`.
- Still accepted: `--ocr-mode on|off`, `--report FILE.json`.

## 3. Changes implemented

- **Persistent Docling worker** (`core/docling_worker.py --serve`, `core/engine.py`):
  - Runs inside Docling's own Python, imports Docling once and loads the models once per batch.
  - Converts one document per request in exactly **one pass**, Markdown export only.
  - After each document it releases the result objects (`del` + `gc.collect()`) and reports its
    memory.
  - It is recycled (fresh process) when it holds more than 25 % of RAM (clamped 1.5–4 GB) after a
    document, after a crash, and after a cancellation. Cancellation kills only the worker's own
    process tree.
  - It is shut down at the end of the batch, and keeps at most one set of models (switching OCR
    on/off drops the previous set).
- **Docling configuration (V1 parity, verified byte-identical, see §5):**
  - standard pipeline, threaded docling-parse backend
  - TableFormer accurate with cell matching
  - picture images at scale 2, referenced PNG
  - `do_ocr` only when ticked
  - code, formula, picture classification/description and chart enrichment explicitly off
  - full-page images not kept in the document object
  - OCR uses Docling's `auto` engine, and the engine actually selected is logged
- **Truthful stages.** Stages come only from explicit worker events: *Preparing → Starting engine
  → Loading models → Converting → Saving Markdown and images → Checking output*. *Starting engine*
  and *Loading models* appear only when that really happens (first file of a batch, or after a
  recycle). Log lines can no longer change the stage, and RapidOCR's "empty result" warnings are
  demoted to debug.
- **Fallback engine.** If Docling's Python can't be located, each PDF is converted with the V1
  command (`docling convert … --to md`).
- **Warnings instead of extra files:**
  - "little text, consider OCR" when OCR is off (hint only; never an automatic second conversion)
  - tables with merged cells (page list, from Docling's table cells; no HTML)
  - missing images
  - unreadable glyphs
- **GPU (optional, Settings dialog):**
  - Device choice Auto/CPU/GPU.
  - NVIDIA detection via `nvidia-smi`; the PyTorch build is read from `torch/version.py` (CUDA
    version or `None`) without importing torch.
  - *Test GPU* runs a real tiny conversion on CUDA. Auto uses the GPU only after that test
    succeeded for the same GPU/PyTorch/Docling combination.
  - A CUDA out-of-memory error, or CUDA being unavailable in the worker, re-runs that document on
    the CPU with a warning.
- **Simplified GUI:**
  - Add PDFs / Add Folder (+ subfolders), Remove Selected, Clear List.
  - A three-column list (File, Status, Time).
  - Output beside source or one folder, plus the OCR checkbox.
  - Convert / Cancel Current / Stop Batch, a completed-file count, Open Output Folder / Markdown,
    and a collapsible log.
  - Technical options moved to *Settings…*.
- **Reliability fixes found while testing:**
  - The persistent worker stayed inside the previous job's staging folder, so Windows refused to
    delete `.d2md_staging`. The worker now returns to a neutral folder after every job and starts
    in `%TEMP%`.
  - A cancel race could hand the next file to the dying worker. Cancellation now waits for the
    process to exit.
  - The laptop went to sleep in the middle of a benchmark (event log: sleep 12:02:20, wake
    12:12:12) and the worker start timeout fired on resume. The timeout now counts awake time
    only, and during a batch the app asks Windows not to idle-sleep (`SetThreadExecutionState`,
    released afterwards; lid-close still sleeps).
  - CPU-only PyTorch detection: the package metadata says `2.14.0`, so the check now reads
    `torch/version.py` instead.

## 4. Measurements (same PDFs, same laptop, isolated settings, sequential runs)

"Processing" is Docling's own *Processing document → Finished converting* time. Memory is the
peak over the app's whole process tree (app + Docling worker), sampled every 0.5 s.

| Run | Total wall time | Docling processing | Launch → processing start | Peak RSS | Peak private |
|---|---|---|---|---|---|
| **WS2812B, 18 p** — V1 (OCR off) | **97.9 s** | 69.1 s | 23.7 s | 1850 MB | 2620 MB |
| V2 Auto (OCR on) | **1030.9 s** | 986.8 s | 33.7 s | 2427 MB | 3478 MB |
| V2 OCR forced off | 121.2 s | 82.9 s | 28.5 s | 1835 MB | 2584 MB |
| **2.1, single file** | **104.8 s** | 73.0 s | 27.1 s | 1888 MB | 2606 MB |
| **74AHCT1G125GV, 14 p** — V1 | 257.1 s | 203.1 s | 50.1 s | 1784 MB | 2498 MB |
| V2 Auto (stayed off) | 225.1 s | 185.9 s | 31.8 s | 1886 MB | 2609 MB |
| **2.1, single file** | 205.8 s | 175.6 s | 26.8 s | 1789 MB | 2494 MB |
| **IQS7222C, 59 p** — V1 | 448.1 s | 407.4 s | 35.8 s | 2704 MB | 3424 MB |

**Batch of the three files above, 2.1 (one worker):**
- **743.9 s** total, vs **803.1 s** for the same files as three separate V1 runs (−7 %).
- Per file: WS2812B 105.7 s (including engine start 14.8 s + model load 11.3 s), 74AHCT 177.6 s
  and IQS7222C 458.0 s (no start-up or model load for these two).
- Peak 2729 MB RSS, during the 59-page file; V1's peak on that file alone was 2704 MB.
- Worker memory after each file: **907 → 931 → 988 MB**. Memory is released back to the model
  footprint after every document, with no accumulation beyond about +40 MB per file.

**Packaged EXE, warm disk cache:**
- First file: engine start 7.2 s + model load 5.5 s.
- 74AHCT as the second file: 142.2 s, with no start-up cost.

**OCR explicitly ticked** (synthetic 3-page scan of the LM22679 datasheet): 167.2 s total (144 s
OCR conversion), peak 1876 MB. The log records "Auto OCR model selected rapidocr with torch".

**Summary:**
- The 15-minute case is fixed by not running OCR unless asked: 1030.9 s → 104.8 s on the same
  PDF (~10×). That puts it back at V1 speed, and the output is identical to V1.
- Per-document speed with OCR off equals V1; the pipeline is the same, and differences are within
  the run-to-run noise.
- Batches save engine start-up and model loading for every file after the first. Measured at about
  13–26 s per file (7 % on the 3-file batch above). This is a modest, real gain, not a large
  speed-up.
- Peak memory per document equals V1's. It's document-dependent (1.8–2.7 GB), not version-dependent,
  and it doesn't grow across a batch. No further memory reduction was attempted beyond releasing
  results and keeping one model set: Docling keeps rendered page images while exporting figures,
  regardless of `generate_page_images`.

**GPU:** not measurable on this laptop. Docling's PyTorch is a CPU-only build (`2.14.0+cpu`) and
the MX110 is compute capability 5.0 with 2 GB. No CUDA PyTorch was installed (per instructions).
The app reports this, keeps Auto on the CPU and disables *Test GPU*. The CUDA request path was
exercised against real Docling: the worker reported "CUDA is not available in Docling's PyTorch
(2.14.0+cpu)" and the document was converted on the CPU with a warning.

## 5. Extraction checks

**Output parity with V1.** Markdown and every PNG are byte-identical between 2.1 and V1 for all
three real datasheets: WS2812B (53 images), 74AHCT1G125GV (11 images) and IQS7222C (164 images).
That holds for single runs, the batch run, and the packaged EXE (74AHCT). Nothing was dropped to
gain speed.

**Spot check against the PDF text layer** (WS2812B):
- **Correct, in the right Min/Typ/Max/Unit columns:**
  - Absolute maximum ratings: VDD 3.5–5.5 V, VOUT 13 V, Topr −40…+85 ℃.
  - Optical table: IV 50/–/150, 200/–/400, 50/–/150 mcd; λD 620–630, 520–530, 465–475 nm;
    λP 625/525/470 nm.
  - Electrical: IDD 0.5 mA, ILED 5 mA, VIL 0.25·VDD, fDATA 800 kHz, fPWM 1.5 kHz, tPLH/tPHL 0.21 µs,
    tTLH/tTHL 0.018 µs.
- **Discrepancies (Docling, identical in V1):**
  - The Unicode minus "−0.5" is written "- 0.5".
  - Merged cells are repeated: the Topr range also appears in the Unit column, and "260°C For |
    6 Seconds" is split over two columns.
  - VIH reads "0.5VD D", as split in the PDF's own text layer.

  The new merged-cell warning names the affected pages.

**OCR sample:** the main heading, "Wide input voltage range: 4.5 V to 42 V" and the pin section were
recovered from the scanned pages. Some words are joined ("switchingregulator").

The structural checks (UTF-8, relative image links inside the folder, no base64 or absolute
paths, tables/headings kept, source unchanged) passed on all runs. They don't prove extraction
accuracy.

## 6. Tests

`pytest`: **100 passed**, run standalone and inside `build.ps1`. The new or updated tests use real
generated PDFs and a fake worker that speaks the real JSON protocol. They prove orchestration, not
accuracy.

**OCR and passes:**
- OCR off by default; on only when requested.
- No HTML/JSON/report/manifest files (the output folder contains exactly `.md` + `images/`).
- Exactly one conversion request per PDF.
- One worker start for a 3-file batch; worker recycled when over the memory limit.

**Output:**
- Separate folders for same-named PDFs and for Unicode/space paths.
- Valid image links.
- Versioned folders with user files untouched.

**Failures and cancellation:**
- Failure followed by success; a corrupt PDF never reaches the engine.
- A worker crash is followed by a fresh worker.
- Partial conversion → *Incomplete*, kept in `NAME_incomplete`, earlier output untouched.
- Cancel Current kills the worker and continues; Stop Batch.

**GPU:**
- Out-of-memory → CPU fallback.
- Device logic: CPU-only PyTorch, verified vs unverified GPU, no GPU.

**Other:**
- Little-text hint without a second pass.
- Queue recovery and v2.0 list/settings migration.
- CLI exit codes and rejected options.
- GUI wiring (convert, restore, remove/clear, log toggle).

**Real-Docling integration runs:**
- The benchmarks above (V1, V2 and 2.1 on 3 datasheets).
- A 3-file batch for memory growth.
- An OCR run.
- A CUDA-unavailable fallback.
- A **GUI run with real Docling**:
  - The two same-named PDFs went to `TVS/` and `TVS_2/`.
  - The stage sequence was Starting engine → Loading models → Converting → Saving → Converting →
    Saving → Checking output.
  - Cancel Current on the third file left it *Cancelled* with no folder published, and no worker
    process remained.
- Packaged EXEs: `--self-test` OK; a CLI conversion of 2 files with output identical to V1; the
  windowed EXE GUI launched with Docling detected.

## 7. Build

`build\build.ps1 -Mode Both`:
- `dist\Datasheet2MD.exe` (windowed) and `dist\Datasheet2MD-Debug.exe` (console), 10.5 MB each
  (V2: 14.2 MB).
- Docling, PyTorch and models are not bundled. The EXE needs a local Docling installation (README
  "Setup").

## 8. Remaining limitations

- **GPU acceleration is untested on real CUDA hardware.** It needs a CUDA PyTorch build that
  supports the GPU, and the MX110 (compute 5.0, 2 GB) likely isn't supported by current CUDA
  wheels. The OOM fallback was tested with the fake worker and the CUDA-unavailable path with real
  Docling.
- **Table-heavy pages stay slow on a laptop CPU.** TableFormer *accurate* takes several seconds
  per table, so 74AHCT needs ~140–260 s for 14 pages. Switching to *fast* tables would trade
  accuracy for speed, so it wasn't done.
- **Merged cells and glyph splits** are Docling limitations (warned, not repaired).
- **Drag & drop** wasn't exercised with a physical drag. Dropped paths go through the same tested
  `add_paths` code.
- **OCR quality and speed** depend on the installed engine. Only RapidOCR (torch) was available,
  so no engine comparison was possible.
