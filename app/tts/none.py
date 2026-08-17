from __future__ import annotations

from .base import TTSResult


class NoneTTSProvider:
    def generate(self, text: str) -> TTSResult:
        return TTSResult(
            success=False,
            error_message="TTS_PROVIDER=none does not generate audio.",
        )
