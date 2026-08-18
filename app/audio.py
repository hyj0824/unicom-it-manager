from __future__ import annotations

"""本地音频：播放封装与话术 WAV 存储规范。

话术生成 WAV 的目录、命名、格式与覆盖约定（产品规范见
docs/callback-demo-plan.md「音频与 TTS」，README「话术音频」）：

- 目录：`data/audio/`（与数据库同级的 data 目录），备份任务把该目录整体
  打包进归档（见 app/services/backups.py）。
- 命名：`script-{话术id}-{话术正文sha1前12位}.wav`。正文变化会得到新文件名，
  同一正文重复生成命中同名文件（即缓存）；重新生成采用原子覆盖，不会出现
  半截文件。
- 格式约定：8000 Hz / 16bit / mono（与现有测试音一致，可直接被 `aplay`
  播放；Provider 负责产出符合该约定的 WAV）。
- 覆盖策略：`write_wav_atomic` 先写同目录临时文件并 fsync，再 `os.replace`
  原子替换；异常时清理临时文件。

Web 试听只允许读取本目录下的 WAV（`resolve_audio_file`），防止路径穿越。
"""

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import BASE_DIR

# 话术 WAV 存储根目录（备份归档中的 audio/ 即此目录）。
AUDIO_DIR = BASE_DIR / "data" / "audio"
# 话术 WAV 固定格式约定：与测试音一致，8kHz / 16bit / mono。
AUDIO_SAMPLE_RATE_HZ = 8000
AUDIO_CHANNELS = 1
AUDIO_BITS_PER_SAMPLE = 16


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


# ---------------------------------------------------------------- 存储规范


def script_audio_path(script_id: int, body: str) -> Path:
    """话术正文对应的规范 WAV 路径：script-{id}-{sha1(body)[:12]}.wav。

    正文不变则路径不变（缓存命中）；正文变化则生成新文件。
    """
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return AUDIO_DIR / f"script-{script_id}-{digest}.wav"


def write_wav_atomic(target: Path, data: bytes) -> None:
    """原子写入 WAV：同目录临时文件 + fsync + os.replace。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install_wav(source: Path, target: Path) -> None:
    """把 Provider 输出的 WAV 原子复制到规范路径。"""
    write_wav_atomic(target, source.read_bytes())


def resolve_audio_file(filename: str) -> Path | None:
    """把客户端文件名解析为 AUDIO_DIR 下的真实 WAV；越界、非法或不存在返回 None。

    防护：拒绝绝对路径、含 `..` 的路径、非 .wav 后缀，并要求最终解析结果
    必须位于 AUDIO_DIR 之内（symlink 穿透由 resolve() 后 relative_to 校验）。
    """
    if not filename or "\x00" in filename:
        return None
    parts = Path(filename).parts
    if not parts or parts[0] in {"/", "\\"} or ".." in parts:
        return None
    root = AUDIO_DIR.resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix.lower() != ".wav" or not candidate.is_file():
        return None
    return candidate
