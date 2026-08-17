from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlaybackResult:
    success: bool
    returncode: int
    message: str = ""


def play_wav(wav_path: str, audio_device: str) -> PlaybackResult:
    path = Path(wav_path)
    if not path.exists():
        return PlaybackResult(False, 1, f"WAV file does not exist: {wav_path}")

    cmd = ["aplay"]
    if audio_device:
        cmd.extend(["-D", audio_device])
    cmd.append(str(path))

    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    message = completed.stderr.strip() or completed.stdout.strip()
    return PlaybackResult(completed.returncode == 0, completed.returncode, message)
