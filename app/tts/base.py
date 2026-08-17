from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TTSResult:
    success: bool
    wav_path: str = ""
    error_message: str = ""


class TTSProvider(Protocol):
    def generate(self, text: str) -> TTSResult:
        """Generate a WAV file from text and return its local path."""
