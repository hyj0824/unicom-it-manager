from __future__ import annotations

from .base import TTSResult


class NoneTTSProvider:
    """离线默认 Provider：不生成音频，话术任务因此直接失败并记录原因。"""

    output_suffix = ".wav"

    def generate(self, text: str) -> TTSResult:
        return TTSResult(
            success=False,
            error_message="TTS_PROVIDER=none does not generate audio.",
        )
