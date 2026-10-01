import core.gpu as gpu
from core.docling_runner import DoclingInfo


def _info(cuda):
    return DoclingInfo(True, "docling.exe", "2.131.0", python="py.exe", backend="worker",
                       probe={"torch": "2.14.0+cpu" if not cuda else "2.14.0+cu121", "torch_cuda": cuda,
                              "torch_cpu_only": not cuda})


MX110 = [gpu.GpuInfo("NVIDIA GeForce MX110", 2048, "5.0", "581.57")]


def test_cpu_only_pytorch_never_uses_gpu(monkeypatch):
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: MX110)
    info = _info(None)
    st = gpu.gpu_status(info)
    assert not st.usable and "CPU-only" in st.reason and "MX110" in st.reason
    for setting in ("auto", "gpu"):
        assert gpu.resolve_device(setting, info, "anything", st)[0] == "cpu"


def test_auto_uses_gpu_only_after_verified_test(monkeypatch):
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: MX110)
    info = _info("12.1")
    st = gpu.gpu_status(info)
    assert st.usable
    assert gpu.resolve_device("auto", info, "", st)[0] == "cpu"
    assert gpu.resolve_device("auto", info, gpu.signature(info, st), st)[0] == "cuda"
    assert gpu.resolve_device("auto", info, "other gpu|torch x|docling y", st)[0] == "cpu"
    assert gpu.resolve_device("gpu", info, "", st)[0] == "cuda"
    assert gpu.resolve_device("cpu", info, gpu.signature(info, st), st)[0] == "cpu"


def test_no_nvidia_gpu(monkeypatch):
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: [])
    st = gpu.gpu_status(_info("12.1"))
    assert not st.usable and "No NVIDIA GPU" in st.reason
    assert gpu.resolve_device("gpu", _info("12.1"), "", st)[0] == "cpu"
