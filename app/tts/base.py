from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TTSResult:
    success: bool
    audio_path: str = ""
    error_message: str = ""


class TTSProvider(Protocol):
    """话术音频 Provider：输入正文，输出本地音频文件路径。

    - `output_suffix`：生成的音频扩展名（如 `.wav` / `.mp3`），生成链路
      据此确定规范存储路径的后缀。
    - `generate`：同步调用，产出本地音频文件并返回其路径；失败时返回
      `success=False` 与原因。播放端用 ffplay 直接播放，格式不做强制转换。
    """

    output_suffix: str

    def generate(self, text: str) -> TTSResult:
        """Generate an audio file from text and return its local path."""
