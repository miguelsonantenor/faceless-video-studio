"""Synthetic stand-ins for the 7 acceptance test clips (FS section 7), made with FFmpeg.

landscape, portrait, no sound, 4K, variable-frame-rate phone clip (rotated), 8-second
clip and 3-minute clip; plus a clip with real speech made by Windows text-to-speech,
for the Whisper test. Generated once into ``tests/fixtures/media`` (git-ignored).

Run directly to (re)build them:  uv run python -m tests.media.make_clips
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "media"
SPEECH_TEXT = ("The volcano makes its own lightning. "
               "Tiny pieces of rock crash into each other and build up an electric charge.")

ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "96k"]


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _landscape(out: Path) -> None:
    """12 s 1920×1080, cuts at 4 s and 8 s, a 440 Hz tone."""
    s = "1920x1080"
    _ff("-f", "lavfi", "-i", f"testsrc2=s={s}:r=30:d=4",
        "-f", "lavfi", "-i", f"smptebars=s={s}:r=30:d=4",
        "-f", "lavfi", "-i", f"color=c=orange:s={s}:r=30:d=4",
        "-f", "lavfi", "-i", "sine=f=440:d=12:sample_rate=48000",
        "-filter_complex",
        "[1]noise=alls=25:allf=t[b];[2]noise=alls=25:allf=t[c];"
        "[0][b][c]concat=n=3:v=1:a=0[v];[3]volume=0.1[a]",
        "-map", "[v]", "-map", "[a]", *ENC, *AAC, str(out))


def _portrait(out: Path) -> None:
    _ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=10",
        "-f", "lavfi", "-i", "sine=f=330:d=10:sample_rate=48000",
        "-af", "volume=0.1", *ENC, *AAC, str(out))


def _nosound_8s(out: Path) -> None:
    """8 s, no audio stream: 2 s black, 3 s moving, 3 s frozen green."""
    s = "1280x720"
    _ff("-f", "lavfi", "-i", f"color=c=black:s={s}:r=30:d=2",
        "-f", "lavfi", "-i", f"testsrc2=s={s}:r=30:d=3",
        "-f", "lavfi", "-i", f"color=c=green:s={s}:r=30:d=3",
        "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-an", *ENC, str(out))


def _uhd(out: Path) -> None:
    _ff("-f", "lavfi", "-i", "testsrc2=s=3840x2160:r=30:d=3",
        "-f", "lavfi", "-i", "sine=f=550:d=3:sample_rate=48000",
        "-af", "volume=0.1", *ENC, *AAC, str(out))


def _phone_vfr(out: Path, work: Path) -> None:
    """Variable frame rate (frames dropped mid-clip) and a 90° display rotation."""
    tmp = work / "vfr_tmp.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
        "-f", "lavfi", "-i", "sine=f=660:d=6:sample_rate=48000",
        "-vf", "select='not(between(n\\,45\\,135)*mod(n\\,3))'", "-fps_mode", "vfr",
        "-af", "volume=0.1", *ENC, *AAC, str(tmp))
    _ff("-display_rotation", "90", "-i", str(tmp), "-c", "copy", str(out))
    tmp.unlink()


def _long_3min(out: Path) -> None:
    _ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=180",
        "-f", "lavfi", "-i", "sine=f=440:d=180:sample_rate=48000",
        "-af", "volume=0.1", *ENC, *AAC, str(out))


def _speech(out: Path, work: Path) -> bool:
    """A clip whose audio is real speech, made with Windows text-to-speech."""
    if sys.platform != "win32":
        return False
    wav = work / "speech.wav"
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SetOutputToWaveFile('{wav}');"
        f"$s.Speak('{SPEECH_TEXT}');$s.Dispose()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    if r.returncode != 0 or not wav.exists():
        return False
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30", "-i", str(wav),
        "-shortest", *ENC, *AAC, str(out))
    wav.unlink()
    return True


CLIPS = {
    "landscape.mp4": _landscape,
    "portrait.mp4": _portrait,
    "nosound_8s.mp4": _nosound_8s,
    "uhd_4k.mp4": _uhd,
    "phone_vfr.mov": _phone_vfr,
    "long_3min.mp4": _long_3min,
    "speech.mp4": _speech,
}


def ensure_clips(folder: Path = FIXTURES) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg is needed to build the test clips")
    folder.mkdir(parents=True, exist_ok=True)
    work = folder / "_work"
    work.mkdir(exist_ok=True)
    made: dict[str, Path] = {}
    for name, fn in CLIPS.items():
        out = folder / name
        if not out.exists():
            ok = fn(out, work) if fn in (_phone_vfr, _speech) else fn(out)
            if ok is False:
                continue
        made[name] = out
    shutil.rmtree(work, ignore_errors=True)
    return made


if __name__ == "__main__":
    for name, path in ensure_clips().items():
        print(f"{name}: {path.stat().st_size / 1e6:.1f} MB")
