"""Optional GPU acceleration: detection, verification and device choice.

Rules:
* CPU is always supported and is the default.
* "GPU" is only usable when an NVIDIA GPU is present AND Docling's PyTorch is
  a CUDA build that supports it. Nothing is installed or changed here.
* "Auto" picks the GPU only after a real test conversion succeeded on this
  exact combination of GPU, PyTorch and Docling (recorded in settings as
  ``gpu_verified``); otherwise Auto means CPU.
* During conversion, a CUDA out-of-memory error makes that document re-run on
  the CPU (see converter), with a warning.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .docling_runner import CREATE_NO_WINDOW, DoclingInfo


@dataclass
class GpuInfo:
    name: str
    memory_mb: Optional[int] = None
    compute_capability: Optional[str] = None
    driver: Optional[str] = None


@dataclass
class GpuStatus:
    gpus: List[GpuInfo] = field(default_factory=list)
    torch_version: Optional[str] = None
    torch_cpu_only: Optional[bool] = None
    usable: bool = False
    reason: str = ""

    @property
    def gpu(self) -> Optional[GpuInfo]:
        return self.gpus[0] if self.gpus else None


def detect_nvidia() -> List[GpuInfo]:
    """NVIDIA GPUs via nvidia-smi (installed with the driver). Empty if none/unknown."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4 and parts[0]:
            try:
                mem = int(float(parts[1]))
            except ValueError:
                mem = None
            gpus.append(GpuInfo(parts[0], mem, parts[2], parts[3]))
    return gpus


def gpu_status(info: Optional[DoclingInfo]) -> GpuStatus:
    st = GpuStatus(gpus=detect_nvidia())
    if info and info.probe:
        st.torch_version = info.probe.get("torch")
        st.torch_cpu_only = bool(info.probe.get("torch_cpu_only"))
    if not st.gpus:
        st.reason = "No NVIDIA GPU detected (nvidia-smi not found or no GPU)."
    elif not info or not info.available:
        st.reason = "Docling is not available."
    elif not st.torch_version:
        st.reason = "PyTorch was not found in Docling's environment."
    elif st.torch_cpu_only:
        st.reason = (f"Docling's PyTorch {st.torch_version} is a CPU-only build, so the "
                     f"{st.gpu.name} cannot be used. A CUDA build of PyTorch that supports this GPU "
                     f"(compute capability {st.gpu.compute_capability}) would be needed.")
    else:
        st.usable = True
        st.reason = (f"{st.gpu.name} ({st.gpu.memory_mb} MB) with PyTorch {st.torch_version}: "
                     "use 'Test GPU' to verify it with a real conversion.")
    return st


def signature(info: Optional[DoclingInfo], st: GpuStatus) -> str:
    gpu = st.gpu.name if st.gpu else "none"
    return f"{gpu}|torch {st.torch_version}|docling {info.version if info else None}"


def resolve_device(setting: str, info: Optional[DoclingInfo], verified: str,
                   status: Optional[GpuStatus] = None) -> tuple:
    """(device, explanation). device is "cpu" or "cuda"."""
    setting = (setting or "auto").lower()
    if setting == "cpu":
        return "cpu", "CPU (selected)"
    st = status or gpu_status(info)
    if setting == "gpu":
        if st.usable:
            return "cuda", f"GPU (selected): {st.gpu.name}"
        return "cpu", f"CPU - GPU selected but not usable: {st.reason}"
    if st.usable and verified and verified == signature(info, st):
        return "cuda", f"GPU (auto, verified): {st.gpu.name}"
    if st.usable:
        return "cpu", "CPU (auto: GPU not verified yet - run 'Test GPU' in Settings)"
    return "cpu", "CPU (auto)" + (f" - {st.reason}" if st.gpus else "")


def test_gpu(info: DoclingInfo, on_line=None) -> tuple:
    """Convert a tiny generated PDF on the GPU. Returns (ok, message)."""
    from .engine import EngineError, WorkerEngine
    from .sample_pdf import make_pdf

    st = gpu_status(info)
    if not st.usable:
        return False, st.reason
    if info.backend != "worker":
        return False, "The GPU test needs the persistent worker (Docling's Python was not found)."
    with tempfile.TemporaryDirectory(prefix="d2md_gputest_") as tmp:
        make_pdf(Path(tmp) / "source.pdf", [{"text": "GPU test page\nVDD 3.3 V\nRegister 0x1F"}])
        eng = WorkerEngine(info)
        try:
            res = eng.convert({"id": "gputest", "workdir": tmp, "source": "source.pdf", "markdown": "source.md",
                               "ocr": False, "device": "cuda"}, on_line or (lambda s: None), lambda s: None)
        except EngineError as exc:
            return False, f"GPU test failed: {exc}"
        finally:
            eng.close()
        if res.status == "success" and (Path(tmp) / "source.md").is_file():
            return True, f"GPU test conversion succeeded on {st.gpu.name} ({res.timings.get('convert_s')} s)."
        return False, "GPU test failed: " + ("; ".join(res.errors) or res.status)
