from __future__ import annotations

"""播放封装（系统 ffmpeg → ALSA）单元测试：mock subprocess，不真实播放。

覆盖：ffmpeg 命令构造（-f alsa + 设备名）、文件缺失、ffmpeg 未安装、非零
退出码与 stderr 透传。
"""

import pytest

import app.audio as audio_module


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, results: list[_FakeCompleted]) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(audio_module.subprocess, "run", run)
    return calls


def test_play_audio_builds_ffmpeg_alsa_command(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    audio = tmp_path / "greeting.mp3"
    audio.write_bytes(b"ID3")
    calls = _patch_run(monkeypatch, [_FakeCompleted()])

    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert result.success
    assert result.returncode == 0
    assert calls == [
        [
            "/usr/bin/ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-i", str(audio),
            "-f", "alsa", "plughw:1,0",
        ]
    ]


def test_play_audio_default_device_when_unset(monkeypatch, tmp_path) -> None:
    """未配置 AUDIO_DEVICE 时输出到 ALSA 默认设备。"""
    audio = tmp_path / "greeting.wav"
    audio.write_bytes(b"RIFF")
    calls = _patch_run(monkeypatch, [_FakeCompleted()])

    result = audio_module.play_audio(str(audio), "")

    assert result.success
    assert calls[0][-1] == "default"


def test_play_audio_missing_file_fails_without_running(monkeypatch) -> None:
    calls = _patch_run(monkeypatch, [])
    result = audio_module.play_audio("/no/such/file.mp3", "plughw:1,0")

    assert not result.success
    assert result.returncode == 1
    assert "does not exist" in result.message
    assert calls == []


def test_play_audio_missing_ffmpeg_reports_install_hint(monkeypatch, tmp_path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(audio_module.shutil, "which", lambda name: None)

    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert not result.success
    assert result.returncode == 127
    assert "apt install ffmpeg" in result.message


def test_play_audio_nonzero_exit_passes_stderr(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    calls = _patch_run(
        monkeypatch,
        [_FakeCompleted(returncode=1, stderr="ALSA function snd_pcm_open failed")],
    )

    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert not result.success
    assert result.returncode == 1
    assert result.message == "ALSA function snd_pcm_open failed"
    assert calls[0][0] == "/usr/bin/ffmpeg"
