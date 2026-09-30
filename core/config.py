"""Application constants, Docling defaults and persisted user settings."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

APP_NAME = "Datasheet2MD"
APP_VERSION = "1.0.0"
APP_SUBTITLE = "Electronic Datasheet → Markdown"

IMAGES_DIRNAME = "images"
STAGING_DIRNAME = ".d2md_work"
STAGING_PDF_NAME = "source.pdf"  # ASCII-only copy handed to Docling
DEBUG_REPORT_NAME = "_datasheet2md_debug_report.json"

# Environment passed to the Docling subprocess. The Hugging Face settings keep
# any model download on plain HTTP with generous timeouts (Xet transfers have
# failed on this machine before); UTF-8 settings make log capture reliable.
DOCLING_ENV_OVERRIDES = {
    "HF_HUB_DISABLE_XET": "1",
    "HF_HUB_DOWNLOAD_TIMEOUT": "600",
    "HF_HUB_ETAG_TIMEOUT": "60",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
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

# Minimum free disk space before starting (bytes).
MIN_FREE_BYTES = 50 * 1024 * 1024


def resource_path(relative: str) -> Path:
    """Locate a bundled resource both from source and from a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def app_data_dir() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(root) / APP_NAME


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def default_models_dir() -> Path:
    return Path.home() / ".cache" / "docling" / "models"


def models_dir_is_complete(path: Path) -> bool:
    return all((path / rel).is_file() for rel in REQUIRED_MODEL_FILES)


def resolve_artifacts_path(setting: str = "auto") -> Optional[Path]:
    """Return the model folder to pass as ``--artifacts-path`` or None.

    ``setting`` is ``"auto"`` (use DOCLING_ARTIFACTS_PATH or the default
    docling-tools download folder when complete), ``"none"`` (never pass it),
    or an explicit folder path.
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
    last_input_dir: str = ""
    last_output_dir: str = ""  # "" means "same directory as source PDF"
    ocr_enabled: bool = False
    llm_cleanup: bool = True
    debug_mode: bool = False
    artifacts_path: str = "auto"
    docling_path: str = ""  # optional explicit docling.exe override

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = path or settings_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        clean = {}
        for key, value in data.items():
            if key not in known:
                continue
            default = getattr(cls(), key)
            if isinstance(default, bool) and isinstance(value, bool):
                clean[key] = value
            elif isinstance(default, str) and isinstance(value, str):
                clean[key] = value
        return cls(**clean)

    def save(self, path: Optional[Path] = None) -> None:
        path = path or settings_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # settings are a convenience; never fail the app over them
