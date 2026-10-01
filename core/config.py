"""Application constants, Docling defaults and persisted user settings."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

APP_NAME = "Datasheet2MD"
APP_VERSION = "2.1.0"
APP_SUBTITLE = "Electronic Datasheet → Markdown"

IMAGES_DIRNAME = "images"
STAGING_DIRNAME = ".d2md_staging"  # per-output-root parent of the per-job staging folders
STAGING_PDF_NAME = "source.pdf"  # ASCII-only copy handed to Docling

# Docling version this release was verified against (other 2.x versions are used as-is).
DOCLING_TESTED_VERSION = "2.131.0"

# Environment passed to the Docling subprocess. The Hugging Face settings keep
# any model download on plain HTTP with generous timeouts; UTF-8 settings make
# log capture reliable.
DOCLING_ENV_OVERRIDES = {
    "HF_HUB_DISABLE_XET": "1",
    "HF_HUB_DOWNLOAD_TIMEOUT": "600",
    "HF_HUB_ETAG_TIMEOUT": "60",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "PYTHONUNBUFFERED": "1",
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "200",
}

# A local model folder is only used when the models the standard pipeline needs
# are really there; otherwise Docling is left to resolve models itself.
REQUIRED_MODEL_FILES = (
    Path("docling-project--docling-layout-heron") / "model.safetensors",
    Path("docling-project--docling-models") / "model_artifacts" / "tableformer" / "accurate"
    / "tableformer_accurate.safetensors",
)

MIN_FREE_BYTES = 50 * 1024 * 1024
DEVICES = ("auto", "cpu", "gpu")


def resource_path(relative: str) -> Path:
    """Locate a bundled resource both from source and from a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def app_data_dir() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(root) / APP_NAME


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def queue_state_path() -> Path:
    return app_data_dir() / "queue.json"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def default_models_dir() -> Path:
    return Path.home() / ".cache" / "docling" / "models"


def models_dir_is_complete(path: Path) -> bool:
    return all((path / rel).is_file() for rel in REQUIRED_MODEL_FILES)


def resolve_artifacts_path(setting: str = "auto") -> Optional[Path]:
    """Return the model folder to pass to Docling (``artifacts_path``) or None.

    ``"auto"``: DOCLING_ARTIFACTS_PATH or the default docling-tools download
    folder when complete; ``"none"``: never; otherwise an explicit folder.
    """
    setting = (setting or "auto").strip()
    if setting.lower() == "none":
        return None
    if setting.lower() != "auto":
        p = Path(os.path.expandvars(os.path.expanduser(setting)))
        return p if p.is_dir() else None
    env = os.environ.get("DOCLING_ARTIFACTS_PATH")
    candidates = ([Path(env)] if env else []) + [default_models_dir()]
    for c in candidates:
        if c.is_dir() and models_dir_is_complete(c):
            return c
    return None


@dataclass
class Settings:
    # main window
    last_input_dir: str = ""
    last_output_dir: str = ""  # output root folder
    output_beside_source: bool = True
    include_subfolders: bool = False
    ocr_enabled: bool = False  # "Scanned PDF / Enable OCR" (same key as v1.0)
    # Settings dialog / settings.json
    device: str = "auto"  # auto | cpu | gpu
    gpu_verified: str = ""  # "<gpu>|<torch>|<docling>" after a successful GPU test conversion
    llm_cleanup: bool = True
    cleanup_i2c: bool = True
    cleanup_page_artifacts: bool = True
    cleanup_captions: bool = True
    cleanup_whitespace: bool = True
    debug_mode: bool = False  # keep the staging folder and log Docling at debug level
    engine_backend: str = "auto"  # auto (persistent worker) | cli (docling.exe per document)
    worker_recycle_mb: int = 0  # 0 = automatic (25 % of RAM, 1.5-4 GB)
    num_threads: int = 0  # 0 = Docling default (4)
    artifacts_path: str = "auto"
    docling_path: str = ""  # optional explicit docling.exe override
    window_geometry: str = ""
    show_log: bool = False

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = path or settings_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        defaults = cls()
        clean = {}
        for key, value in data.items():
            if key not in known:
                continue  # settings of removed features (v2.0 ocr_mode, collision_policy, ...) are ignored
            default = getattr(defaults, key)
            if isinstance(default, bool):
                if isinstance(value, bool):
                    clean[key] = value
            elif isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    clean[key] = value
            elif isinstance(default, str) and isinstance(value, str):
                clean[key] = value
        settings = cls(**clean)
        # v2.0 stored the OCR choice as ocr_mode (and always wrote ocr_enabled=false).
        # Auto OCR was removed in 2.1: only an explicit "on"/"force" keeps OCR enabled.
        if "ocr_mode" in data:
            settings.ocr_enabled = data.get("ocr_mode") in ("on", "force")
        if "output_beside_source" not in data and settings.last_output_dir:
            settings.output_beside_source = False  # v1.0 used a custom output folder
        settings.normalize()
        return settings

    def normalize(self) -> None:
        if self.device not in DEVICES:
            self.device = "auto"
        if self.engine_backend not in ("auto", "worker", "cli"):
            self.engine_backend = "auto"
        self.num_threads = max(0, min(int(self.num_threads), 64))

    def save(self, path: Optional[Path] = None) -> None:
        path = path or settings_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # settings are a convenience; never fail the app over them
