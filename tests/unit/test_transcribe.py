"""Whisper device choice: GPU when it can run there, CPU otherwise."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import ctranslate2
import faster_whisper
import pytest

from fvs.analyze import transcribe
from fvs.analyze.transcribe import CPU, GPU, Whisper, best_device


@pytest.fixture(autouse=True)
def fresh_device(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("PATH", transcribe.os.environ.get("PATH", ""))  # restored afterwards
    best_device.cache_clear()
    yield
    best_device.cache_clear()


def test_no_nvidia_gpu_means_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 0)
    assert best_device() is CPU


@pytest.mark.skipif(sys.platform != "win32", reason="DLL loading is Windows-only")
def test_missing_cuda_libraries_mean_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_dll(name: str) -> None:
        raise OSError(f"{name} not found")

    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(transcribe.ctypes, "WinDLL", no_dll)
    assert best_device() is CPU


def test_gpu_load_failure_falls_back_to_cpu(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    loaded: list[tuple[str, str]] = []

    class FakeModel:
        def __init__(self, path: str, *, device: str, compute_type: str) -> None:
            if device == "cuda":
                raise RuntimeError("CUDA failed with error out of memory")
            loaded.append((device, compute_type))

    monkeypatch.setattr(transcribe, "best_device", lambda: GPU)
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    w = Whisper(tmp_path)
    monkeypatch.setattr(w, "model_path", lambda: tmp_path)
    w._load()
    assert loaded == [("cpu", "int8")] and w.device is CPU


def test_the_cpu_setting_keeps_whisper_off_the_gpu(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    """Settings → Performance: GPU by default, CPU when the creator picks it."""
    loaded: list[str] = []

    class FakeModel:
        def __init__(self, path: str, *, device: str, compute_type: str) -> None:
            loaded.append(device)

    monkeypatch.setattr(transcribe, "best_device", lambda: GPU)
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    setting = {"gpu": True}
    w = Whisper(tmp_path, use_gpu=lambda: setting["gpu"])
    monkeypatch.setattr(w, "model_path", lambda: tmp_path)
    assert w.device is GPU
    w._load()
    setting["gpu"] = False
    assert w.device is CPU  # what the next transcription will use
    w._load()
    w._load()  # loaded once per change, not on every call
    assert loaded == ["cuda", "cpu"]


def test_the_model_is_unloaded_even_after_a_cancel(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    """A cancelled or failed transcription must not keep ~1 GB loaded until FVS closes."""

    class Seg:
        start, end, text, words = 0.0, 1.0, "hi", []

    class FakeModel:
        def __init__(self, path: str, *, device: str, compute_type: str) -> None:
            pass

        def transcribe(self, audio: object, **kw: object) -> tuple:
            info = type("Info", (), {"language": "en", "language_probability": 1.0})
            return iter([Seg(), Seg()]), info

    monkeypatch.setattr(transcribe, "best_device", lambda: CPU)
    monkeypatch.setattr(transcribe, "decode_pcm", lambda *a, **k: [0.0])
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    w = Whisper(tmp_path, idle_unload_s=0.05)
    monkeypatch.setattr(w, "model_path", lambda: tmp_path)
    with pytest.raises(transcribe.JobCancelled):
        w.transcribe(tmp_path / "a.wav", cancelled=lambda: True)
    assert w._model is not None and w._timer is not None  # loaded, unload scheduled
    w._timer.join(2)
    assert w._model is None and w._timer is None
