"""ANA-4: speech in a clip's own audio, with word timings, by local faster-whisper.

The ``small`` model (DS 10.5) lives in ``~/.fvs/models``. It is never downloaded
implicitly: ``fvs whisper-download`` fetches it once. It runs on an NVIDIA GPU in float16
when one and the ``gpu`` extra's CUDA libraries are present, otherwise on the CPU in int8.
The model is loaded on first use and unloaded after a few idle minutes, which also frees
the GPU memory. One transcription runs at a time.
"""

from __future__ import annotations

import ctypes
import functools
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fvs.errors import ErrorCode, FvsError
from fvs.jobs import JobCancelled
from fvs.logs import get_logger
from fvs.media.audio import decode_pcm

MODEL_SIZE = "small"
IDLE_UNLOAD_S = 300.0
SAMPLE_RATE = 16_000
CUDA_DLLS = ("cublas64_12.dll", "cudnn64_9.dll")

log = get_logger(__name__)


@dataclass(frozen=True)
class Device:
    name: str  # "cuda" or "cpu"
    compute_type: str
    x_realtime: float  # seconds of speech transcribed per second of work (estimates only)

    @property
    def label(self) -> str:
        return f"{'GPU' if self.name == 'cuda' else 'CPU'}, {self.compute_type}"


# Measured on 61 s of continuous speech: desktop CPU 129 s, RTX 3060 17 s.
CPU = Device("cpu", "int8", 0.45)
GPU = Device("cuda", "float16", 3.0)


def _cuda_dll_dirs() -> list[Path]:
    """The ``bin`` folders of the nvidia-cublas-cu12 and nvidia-cudnn-cu12 wheels."""
    try:
        import nvidia  # namespace package installed by the wheels
    except ImportError:
        return []
    return [d for root in map(Path, nvidia.__path__)
            for d in (root / "cublas" / "bin", root / "cudnn" / "bin") if d.is_dir()]


@functools.cache
def best_device() -> Device:
    """The GPU when CTranslate2 sees an NVIDIA card and its CUDA libraries load, else CPU."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() == 0:
            return CPU
    except Exception:  # noqa: BLE001 - a broken driver must not stop transcription
        return CPU
    if sys.platform == "win32":
        dirs = _cuda_dll_dirs()
        for d in dirs:
            os.add_dll_directory(str(d))
        # cuDNN loads its sub-libraries by plain name, which searches PATH.
        os.environ["PATH"] = os.pathsep.join([*map(str, dirs), os.environ.get("PATH", "")])
        try:
            for dll in CUDA_DLLS:
                ctypes.WinDLL(dll)
        except OSError:
            return CPU
    return GPU


class Whisper:
    def __init__(self, models_dir: Path, *, idle_unload_s: float = IDLE_UNLOAD_S) -> None:
        self.models_dir = models_dir
        self.idle_unload_s = idle_unload_s
        self._model: Any = None
        self._device: Device | None = None  # set when the model loads
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # ---- model files ---------------------------------------------------------------

    def model_path(self) -> Path | None:
        """The local model folder, or None if it has not been downloaded."""
        try:
            from faster_whisper.utils import download_model
        except ImportError:
            return None
        try:
            return Path(download_model(MODEL_SIZE, cache_dir=str(self.models_dir),
                                       local_files_only=True))
        except Exception:  # noqa: BLE001 - huggingface_hub raises several kinds
            return None

    def download(self) -> Path:
        from faster_whisper.utils import download_model

        self.models_dir.mkdir(parents=True, exist_ok=True)
        return Path(download_model(MODEL_SIZE, cache_dir=str(self.models_dir)))

    @property
    def available(self) -> bool:
        return self.model_path() is not None

    # ---- transcription -------------------------------------------------------------

    def _load(self) -> Any:
        if self._model is None:
            path = self.model_path()
            if path is None:
                raise FvsError(
                    ErrorCode.WHISPER_MODEL_MISSING,
                    "The Whisper speech model is not installed, so speech cannot be "
                    "transcribed.",
                    "Run `fvs whisper-download` once (about 500 MB), then try again.",
                )
            from faster_whisper import WhisperModel

            device = best_device()
            try:
                self._model = WhisperModel(str(path), device=device.name,
                                           compute_type=device.compute_type)
            except (RuntimeError, ValueError) as exc:
                if device is CPU:
                    raise
                log.warning("whisper_gpu_failed", error=str(exc))
                device = CPU
                self._model = WhisperModel(str(path), device="cpu",
                                           compute_type=CPU.compute_type)
            self._device = device
        return self._model

    @property
    def device(self) -> Device:
        """Where transcription runs: the device the model loaded on, else the best one."""
        return self._device or best_device()

    def _schedule_unload(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.idle_unload_s, self.unload)
        self._timer.daemon = True
        self._timer.start()

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def transcribe(self, media: Path, *, language: str | None = None,
                   cancelled: Callable[[], bool] | None = None,
                   progress: Callable[[float], None] | None = None,
                   duration_s: float | None = None) -> dict[str, Any]:
        # Whisper gets samples, not a file, so all decoding stays with the FFmpeg that
        # `fvs doctor` checks (faster-whisper's own PyAV path breaks across PyAV versions).
        audio = decode_pcm(media, SAMPLE_RATE, cancelled)
        with self._lock:
            model = self._load()
            segments_iter, info = model.transcribe(
                audio, language=language, word_timestamps=True, vad_filter=True,
                condition_on_previous_text=False)
            segments = []
            for seg in segments_iter:  # decoding happens lazily, segment by segment
                if cancelled is not None and cancelled():
                    raise JobCancelled()
                segments.append({  # plain floats: faster-whisper returns numpy scalars
                    "start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                    "text": seg.text.strip(),
                    "words": [{"start": round(float(w.start), 3),
                               "end": round(float(w.end), 3),
                               "word": w.word.strip(), "p": round(float(w.probability), 3)}
                              for w in (seg.words or [])],
                })
                if progress is not None and duration_s:
                    progress(min(1.0, seg.end / duration_s))
            self._schedule_unload()
        text = " ".join(s["text"] for s in segments).strip()
        return {
            "model": f"{MODEL_SIZE}-{self.device.compute_type}",
            "device": self.device.name,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "has_speech": bool(text),
            "text": text,
            "segments": segments,
        }
