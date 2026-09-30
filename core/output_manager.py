"""Output folder naming, versioning and safe preparation."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from .config import IMAGES_DIRNAME, STAGING_DIRNAME

PathLike = Union[str, os.PathLike]

# "-Datasheet", "_Datasheet", " Datasheet" (any letter case) at the end of the stem.
_DATASHEET_SUFFIX_RE = re.compile(r"[-_ ]datasheet$", re.IGNORECASE)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ExistsPolicy(Enum):
    NEW_VERSION = "new_version"
    OVERWRITE = "overwrite"
    CANCEL = "cancel"


class OutputError(Exception):
    """Raised when the output location cannot be used."""


def component_folder_name(pdf: PathLike) -> str:
    """Folder name for a datasheet: the PDF stem without a trailing datasheet suffix.

    >>> component_folder_name("BNO080_085-Datasheet.pdf")
    'BNO080_085'
    >>> component_folder_name("STM32F103C8T6.pdf")
    'STM32F103C8T6'
    """
    stem = Path(pdf).stem.strip()
    name = _DATASHEET_SUFFIX_RE.sub("", stem).rstrip(" ._-")
    if not name:
        name = stem  # e.g. "Datasheet.pdf" or "-datasheet.pdf": keep the full stem
    name = name.rstrip(" .")  # Windows forbids trailing dots/spaces
    if not name or name.upper() in _WINDOWS_RESERVED:
        name = f"{name or 'datasheet'}_output"
    return name


def markdown_filename(pdf: PathLike) -> str:
    """Markdown name is the PDF file name with .pdf replaced by .md."""
    return Path(pdf).stem + ".md"


@dataclass(frozen=True)
class OutputPlan:
    source_pdf: Path
    component_dir: Path
    markdown_path: Path
    images_dir: Path
    staging_dir: Path
    existed_before: bool

    @property
    def images_rel(self) -> str:
        return IMAGES_DIRNAME


def output_root_for(pdf: PathLike, output_root: Optional[PathLike]) -> Path:
    """The directory in which the component folder is created."""
    if output_root and str(output_root).strip():
        return Path(output_root).expanduser().resolve()
    return Path(pdf).expanduser().resolve().parent


def base_component_dir(pdf: PathLike, output_root: Optional[PathLike] = None) -> Path:
    return output_root_for(pdf, output_root) / component_folder_name(pdf)


def next_version_dir(base: Path) -> Path:
    """First of base, base_2, base_3, ... that does not exist yet."""
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{base.name}_{n}")
        if not candidate.exists():
            return candidate
        n += 1


def plan_output(
    pdf: PathLike,
    output_root: Optional[PathLike] = None,
    policy: ExistsPolicy = ExistsPolicy.NEW_VERSION,
) -> OutputPlan:
    """Decide where the results go. Nothing is created or deleted here."""
    if policy is ExistsPolicy.CANCEL:
        raise OutputError("Conversion cancelled by user.")
    pdf_path = Path(pdf).expanduser().resolve()
    base = base_component_dir(pdf_path, output_root)
    if base.exists() and not base.is_dir():
        # A *file* with the component name exists: always pick a new name.
        target = next_version_dir(base)
    elif policy is ExistsPolicy.NEW_VERSION:
        target = next_version_dir(base)
    else:
        target = base
    return OutputPlan(
        source_pdf=pdf_path,
        component_dir=target,
        markdown_path=target / markdown_filename(pdf_path),
        images_dir=target / IMAGES_DIRNAME,
        staging_dir=target / STAGING_DIRNAME,
        existed_before=target.exists(),
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def prepare_output(plan: OutputPlan) -> None:
    """Create the component folder; when overwriting, remove only generated files.

    Generated files are the Markdown file, the images/ folder and the staging
    folder. Anything else the user put in the folder is left alone, and the
    source PDF is never deleted.
    """
    src = plan.source_pdf
    if plan.markdown_path.resolve() == src:
        raise OutputError("Refusing to write over the source PDF.")
    for generated in (plan.images_dir, plan.staging_dir):
        if generated.exists() and _is_within(src, generated):
            raise OutputError(
                f"The source PDF is inside '{generated}', which would be replaced. "
                "Move the PDF or choose another output folder."
            )
    try:
        plan.component_dir.mkdir(parents=True, exist_ok=True)
        if plan.existed_before:
            if plan.markdown_path.is_file():
                plan.markdown_path.unlink()
            for generated in (plan.images_dir, plan.staging_dir):
                if generated.is_dir():
                    shutil.rmtree(generated)
                elif generated.exists():
                    generated.unlink()
        plan.staging_dir.mkdir(parents=True, exist_ok=True)
        _hide_on_windows(plan.staging_dir)
    except PermissionError as exc:
        raise OutputError(
            f"Permission denied while preparing '{plan.component_dir}'. "
            "Choose a folder you can write to, or close programs using these files."
        ) from exc
    except OSError as exc:
        raise OutputError(f"Output folder unavailable: {exc}") from exc


def check_writable(directory: Path) -> None:
    """Raise OutputError if a file cannot be created in ``directory``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".d2md_write_test_{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except PermissionError as exc:
        raise OutputError(f"Permission denied: cannot write to '{directory}'.") from exc
    except OSError as exc:
        raise OutputError(f"Output directory unavailable: '{directory}' ({exc}).") from exc


def free_bytes(directory: Path) -> Optional[int]:
    probe = directory
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def discard_output(plan: OutputPlan) -> None:
    """Remove what a failed/cancelled run created (only a folder we created)."""
    shutil.rmtree(plan.staging_dir, ignore_errors=True)
    if not plan.existed_before and plan.component_dir.is_dir():
        if not _is_within(plan.source_pdf, plan.component_dir):
            shutil.rmtree(plan.component_dir, ignore_errors=True)


def _hide_on_windows(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass
