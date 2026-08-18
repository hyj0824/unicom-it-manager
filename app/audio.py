from __future__ import annotations

"""本地音频：播放封装与话术音频存储规范。

话术音频的目录、命名与覆盖约定：

- 目录：`data/audio/`（与数据库同级的 data 目录），备份任务把该目录整体
  打包进归档（见 app/services/backups.py）。
- 命名：`script-{话术id}-{正文sha1前12位}{扩展名}`。正文变化会得到新文件名，
  同一正文重复生成命中同名文件（即缓存）；重新生成采用原子覆盖，不会出现
  半截文件。
- 格式：由 TTS Provider 决定（`output_suffix`，如 `.wav` 或 `.mp3`），
  不做强制转换；`TTS_PROVIDER=none` 的测试音仍为 8kHz/16bit/mono WAV。
- 播放：统一使用 `ffplay`（ffmpeg 套件，必选依赖），支持所有 ffplay 可解
  码的格式；`AUDIO_DEVICE` 在 ffplay 支持 `-audio_device`（ffmpeg ≥ 6.0）
  时生效，旧版本回退到系统默认 ALSA 设备。
- 覆盖策略：`write_wav_atomic` 先写同目录临时文件并 fsync，再 `os.replace`
  原子替换；异常时清理临时文件。

Web 试听只允许读取本目录下的 WAV/MP3（`resolve_audio_file`），防止路径穿越。
"""

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import BASE_DIR

# 话术音频存储根目录（备份归档中的 audio/ 即此目录）。
AUDIO_DIR = BASE_DIR / "data" / "audio"

# 试听/播放允许的音频扩展名。
AUDIO_EXTENSIONS = {".wav", ".mp3"}


@dataclass(frozen=True)
class PlaybackResult:
    success: bool
    returncode: int
    message: str = ""


@lru_cache(maxsize=1)
def _ffplay_supports_audio_device() -> bool:
    """探测本机 ffplay 是否支持 `-audio_device`（ffmpeg ≥ 6.0 新增）。"""
    try:
        completed = subprocess.run(
            ["ffplay", "-hide_banner", "-h"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return "-audio_device" in (completed.stdout + completed.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def play_audio(audio_path: str, audio_device: str) -> PlaybackResult:
    """用 ffplay 播放音频文件；`-nodisp -autoexit` 无窗口、播完即退。"""
    path = Path(audio_path)
    if not path.exists():
        return PlaybackResult(False, 1, f"Audio file does not exist: {audio_path}")

    cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
    if audio_device and _ffplay_supports_audio_device():
        cmd.extend(["-audio_device", audio_device])
    cmd.append(str(path))

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return PlaybackResult(
            False, 127, "ffplay not found; install ffmpeg to provide the ffplay player."
        )
    message = completed.stderr.strip() or completed.stdout.strip()
    return PlaybackResult(completed.returncode == 0, completed.returncode, message)


# ---------------------------------------------------------------- 存储规范


def script_audio_path(script_id: int, body: str, ext: str = ".wav") -> Path:
    """话术正文对应的规范音频路径：script-{id}-{sha1(body)[:12]}{ext}。

    正文不变则路径不变（缓存命中）；正文变化则生成新文件。`ext` 由
    Provider 的 `output_suffix` 决定（如 `.wav` / `.mp3`）。
    """
    if not ext.startswith("."):
        ext = f".{ext}"
    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    return AUDIO_DIR / f"script-{script_id}-{digest}{ext}"


def write_wav_atomic(target: Path, data: bytes) -> None:
    """原子写入音频文件：同目录临时文件 + fsync + os.replace。"""
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
    """把 Provider 输出的音频文件原子复制到规范路径。"""
    write_wav_atomic(target, source.read_bytes())


def resolve_audio_file(filename: str) -> Path | None:
    """把客户端文件名解析为 AUDIO_DIR 下的真实音频文件；越界、非法或不存在返回 None。

    防护：拒绝绝对路径、含 `..` 的路径、非 WAV/MP3 后缀，并要求最终解析
    结果必须位于 AUDIO_DIR 之内（symlink 穿透由 resolve() 后 relative_to
    校验）。
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
    if candidate.suffix.lower() not in AUDIO_EXTENSIONS or not candidate.is_file():
        return None
    return candidate
