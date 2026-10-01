"""Output folder naming, destination reservation, staging and safe publishing.

Safety rules:

* An existing folder is never written into, overwritten or deleted. If the
  folder name is taken, a new version is created (``LM358_2``, ``LM358_3``...).
* Destinations are reserved for the whole batch, so two queued PDFs with the
  same name (from different folders) can never claim the same folder.
* Every document is assembled in its own staging folder on the output drive
  and published with an atomic ``os.rename``, which fails instead of merging
  if the name was taken in the meantime (then the next free version is used).
  Cancelled or failed conversions are never published.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from .config import IMAGES_DIRNAME, STAGING_DIRNAME

PathLike = Union[str, os.PathLike]

# "-Datasheet", "_Datasheet", " Datasheet" (any letter case) at the end of the stem.
_DATASHEET_SUFFIX_RE = re.compile(r"[-_ ]datasheet$", re.IGNORECASE)
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
    "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³",
}
MAX_NAME_CHARS = 120
LEGACY_MAX_PATH = 259  # MAX_PATH (260) minus the terminating NUL


class OutputError(Exception):
    """Raised when the output location cannot be used."""


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
def sanitize_name(name: str, fallback: str = "datasheet") -> str:
    """Make ``name`` a valid Windows file/folder name (never empty, never reserved)."""
    s = _INVALID_CHARS_RE.sub("_", name or "")
    s = s.strip().rstrip(" .")
    if len(s) > MAX_NAME_CHARS:
        s = s[:MAX_NAME_CHARS].rstrip(" .")
    if not s or set(s) <= {"."}:
        s = fallback
    if s.split(".")[0].strip().upper() in _WINDOWS_RESERVED:
        s = f"{s}_"
    return s


def component_folder_name(pdf: PathLike) -> str:
    """Folder name for a datasheet: the PDF stem without a trailing datasheet suffix.

    >>> component_folder_name("BNO080_085-Datasheet.pdf")
    'BNO080_085'
    """
    stem = Path(pdf).stem.strip()
    name = _DATASHEET_SUFFIX_RE.sub("", stem).rstrip(" ._-")
    if not name:
        name = stem  # e.g. "Datasheet.pdf": keep the full stem
    name = sanitize_name(name)
    if name.upper() == STAGING_DIRNAME.upper():
        name += "_output"
    return name


def markdown_filename(pdf: PathLike) -> str:
    """Markdown name is the PDF file name with .pdf replaced by .md."""
    return sanitize_name(Path(pdf).stem) + ".md"


def output_root_for(pdf: PathLike, output_root: Optional[PathLike]) -> Path:
    """The directory in which the component folder is created."""
    if output_root and str(output_root).strip():
        return Path(output_root).expanduser().resolve()
    return Path(pdf).expanduser().resolve().parent


def version_candidates(base: Path, limit: int = 10000) -> Iterable[Path]:
    yield base
    for n in range(2, limit):
        yield base.with_name(f"{base.name}_{n}")


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def long_paths_enabled() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def check_path_lengths(target_dir: Path, markdown_name: str) -> None:
    """Raise OutputError if the output paths would exceed the Windows path limit."""
    if long_paths_enabled():
        return
    longest = max(len(str(target_dir / markdown_name)),
                  len(str(target_dir / IMAGES_DIRNAME / "image_0001.png"))) + len("_999")
    if longest > LEGACY_MAX_PATH:
        raise OutputError(
            f"The output path would be {longest} characters long, which exceeds the Windows limit of "
            f"{LEGACY_MAX_PATH}. Choose a shorter output folder (or rename the PDF), or enable Windows "
            "long paths (LongPathsEnabled).")


# --------------------------------------------------------------------------- #
# Reservations and planning
# --------------------------------------------------------------------------- #
class Reservations:
    """Thread-safe registry of destination folders claimed by queued jobs."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owners: Dict[str, str] = {}

    def reserve(self, path: Path, job_id: str) -> None:
        with self._lock:
            self._owners[_key(path)] = job_id

    def taken_by_others(self, path: Path, job_id: str) -> bool:
        with self._lock:
            owner = self._owners.get(_key(path))
        return owner is not None and owner != job_id

    def release_job(self, job_id: str) -> None:
        with self._lock:
            for k in [k for k, v in self._owners.items() if v == job_id]:
                del self._owners[k]

    def clear(self) -> None:
        with self._lock:
            self._owners.clear()


@dataclass
class DestinationPlan:
    source_pdf: Path
    root: Path
    base_dir: Path
    target_dir: Path
    markdown_name: str
    notes: List[str] = field(default_factory=list)

    @property
    def markdown_path(self) -> Path:
        return self.target_dir / self.markdown_name


def plan_destination(pdf: PathLike, output_root: Optional[PathLike],
                     reservations: Optional[Reservations] = None, job_id: str = "") -> DestinationPlan:
    """Decide (and reserve) where a PDF's results go. Nothing is created or deleted."""
    reservations = reservations or Reservations()
    pdf_path = Path(pdf).expanduser().resolve()
    root = output_root_for(pdf_path, output_root)
    base = root / component_folder_name(pdf_path)
    md_name = markdown_filename(pdf_path)
    target = None
    for candidate in version_candidates(base):
        if not candidate.exists() and not reservations.taken_by_others(candidate, job_id):
            target = candidate
            break
    if target is None:
        raise OutputError(f"Too many existing versions of '{base}'.")
    plan = DestinationPlan(pdf_path, root, base, target, md_name)
    if target != base:
        why = "already used by another PDF in this batch" if not base.exists() else "already exists"
        plan.notes.append(f"'{base.name}' {why}; writing '{target.name}' instead (nothing is overwritten).")
    reservations.reserve(target, job_id)
    check_path_lengths(target, md_name)
    return plan


# --------------------------------------------------------------------------- #
# Staging and publishing
# --------------------------------------------------------------------------- #
def check_writable(directory: Path) -> None:
    """Raise OutputError if a file cannot be created in ``directory``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".d2md_write_test_{os.getpid()}_{secrets.token_hex(3)}"
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


def create_staging(root: Path, job_id: str) -> Path:
    """A unique, hidden per-job working folder on the same drive as the output."""
    parent = root / STAGING_DIRNAME
    try:
        parent.mkdir(parents=True, exist_ok=True)
        _set_hidden(parent)
        safe_id = re.sub(r"[^A-Za-z0-9]", "", job_id)[:8] or "job"
        stage = parent / f"{safe_id}_{os.getpid()}_{secrets.token_hex(3)}"
        stage.mkdir()
    except PermissionError as exc:
        raise OutputError(f"Permission denied: cannot create a working folder in '{root}'.") from exc
    except OSError as exc:
        raise OutputError(f"Cannot create a working folder in '{root}': {exc}") from exc
    return stage


def remove_staging(stage: Path) -> None:
    """Delete a job's own staging folder (never a destination), then the empty parent."""
    if stage.parent.name != STAGING_DIRNAME:
        return  # safety: only ever delete folders created by create_staging
    shutil.rmtree(stage, ignore_errors=True)
    try:
        stage.parent.rmdir()  # only succeeds when empty (another job may still use it)
    except OSError:
        pass


def publish_new(staged: Path, plan: DestinationPlan, reservations: Optional[Reservations] = None,
                job_id: str = "") -> Path:
    """Atomically move a finished staged folder to the planned (or next free) folder.

    ``os.rename`` fails when the target exists, so an existing folder is never merged into.
    """
    reservations = reservations or Reservations()
    for candidate in [plan.target_dir, *version_candidates(plan.base_dir)]:
        if candidate.exists() or reservations.taken_by_others(candidate, job_id):
            continue
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staged, candidate)
        except FileExistsError:
            continue
        except PermissionError:
            if candidate.exists():
                continue
            raise
        reservations.reserve(candidate, job_id)
        return candidate
    raise OutputError(f"No free output folder name for '{plan.base_dir}'.")


def _set_hidden(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass
