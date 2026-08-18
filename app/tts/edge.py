from __future__ import annotations

"""Microsoft Edge 在线 TTS Provider。

免费、无需 API key，但需要能访问微软 Edge TTS 服务的网络（内网离线环境
不可用时会失败并写入 `tts_error`）。输出 24kHz mono MP3，
直接播放，不做转码（格式约定放宽，见 app/audio.py 与 README「话术音频」）。

配置：`TTS_PROVIDER=edge`，可选 `TTS_VOICE`（默认 `zh-CN-XiaoxiaoNeural`）。
"""

import asyncio
import os
import tempfile
from pathlib import Path

from edge_tts import Communicate

from .base import TTSResult


class EdgeTTSProvider:
    output_suffix = ".mp3"
    DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
    GENERATE_TIMEOUT_SECONDS = 60

    def __init__(self, voice: str = "", rate: str = "", volume: str = "") -> None:
        self.voice = voice or self.DEFAULT_VOICE
        self.rate = rate
        self.volume = volume

    def generate(self, text: str) -> TTSResult:
        if not text.strip():
            return TTSResult(success=False, error_message="话术正文为空。")
        fd, tmp_name = tempfile.mkstemp(suffix=self.output_suffix)
        os.close(fd)
        tmp_path = Path(tmp_name)
        ok = False
        try:
            asyncio.run(self._generate_to(text, tmp_path))
            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                return TTSResult(success=False, error_message="edge-tts 未产出音频文件。")
            ok = True
            return TTSResult(success=True, audio_path=str(tmp_path))
        except Exception as exc:  # noqa: BLE001 - 网络/服务异常统一转为失败结果
            return TTSResult(success=False, error_message=f"edge-tts: {type(exc).__name__}: {exc}")
        finally:
            # 失败时清理临时文件；成功路径由 generate_script_audio 原子复制后清理。
            if not ok:
                tmp_path.unlink(missing_ok=True)

    async def _generate_to(self, text: str, path: Path) -> None:
        kwargs: dict[str, str] = {}
        if self.rate:
            kwargs["rate"] = self.rate
        if self.volume:
            kwargs["volume"] = self.volume
        communicate = Communicate(text, self.voice, **kwargs)
        await asyncio.wait_for(
            communicate.save(str(path)), timeout=self.GENERATE_TIMEOUT_SECONDS
        )
