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
