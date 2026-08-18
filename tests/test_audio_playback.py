from __future__ import annotations

"""播放封装（ffplay）单元测试：mock subprocess，不真实播放。

覆盖：ffplay 命令构造（含 -audio_device 探测开关）、文件缺失、ffplay 未
安装、非零退出码与 stderr 透传。
"""

from types import SimpleNamespace

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


def test_play_audio_builds_ffplay_command_with_device(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(audio_module, "_ffplay_supports_audio_device", lambda: True)
    audio = tmp_path / "greeting.mp3"
    audio.write_bytes(b"ID3")
    calls = _patch_run(monkeypatch, [_FakeCompleted()])

    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert result.success
    assert result.returncode == 0
    assert calls == [
        [
            "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
            "-audio_device", "plughw:1,0", str(audio),
        ]
    ]


def test_play_audio_omits_device_when_unsupported(monkeypatch, tmp_path) -> None:
    """旧版 ffplay（< 6.0）不支持 -audio_device，应回退到默认设备。"""
    monkeypatch.setattr(audio_module, "_ffplay_supports_audio_device", lambda: False)
    audio = tmp_path / "greeting.wav"
    audio.write_bytes(b"RIFF")
    calls = _patch_run(monkeypatch, [_FakeCompleted()])

    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert result.success
    assert "-audio_device" not in calls[0]


def test_play_audio_missing_file_fails_without_running(monkeypatch) -> None:
    calls = _patch_run(monkeypatch, [])
    result = audio_module.play_audio("/no/such/file.mp3", "plughw:1,0")

    assert not result.success
    assert result.returncode == 1
    assert "does not exist" in result.message
    assert calls == []


def test_play_audio_ffplay_missing_reports_install_hint(monkeypatch, tmp_path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")

    def run(cmd, **kwargs):
        raise FileNotFoundError("ffplay")

    monkeypatch.setattr(audio_module.subprocess, "run", run)
    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert not result.success
    assert result.returncode == 127
    assert "install ffmpeg" in result.message


def test_play_audio_nonzero_exit_passes_stderr(monkeypatch, tmp_path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    calls = _patch_run(
        monkeypatch,
        [_FakeCompleted(returncode=1, stderr="Output device not found")],
    )

    result = audio_module.play_audio(str(audio), "plughw:1,0")

    assert not result.success
    assert result.returncode == 1
    assert result.message == "Output device not found"
    assert calls[0][0] == "ffplay"


def test_device_support_probe_true_when_flag_present(monkeypatch) -> None:
    audio_module._ffplay_supports_audio_device.cache_clear()
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout="... -audio_device <device> ..."),
    )
    try:
        assert audio_module._ffplay_supports_audio_device() is True
        assert audio_module._ffplay_supports_audio_device() is True  # 缓存
    finally:
        audio_module._ffplay_supports_audio_device.cache_clear()


def test_device_support_probe_false_without_flag(monkeypatch) -> None:
    audio_module._ffplay_supports_audio_device.cache_clear()
    monkeypatch.setattr(
        audio_module.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(stdout="ffplay version 5.1.4 ..."),
    )
    try:
        assert audio_module._ffplay_supports_audio_device() is False
    finally:
        audio_module._ffplay_supports_audio_device.cache_clear()


def test_device_support_probe_false_when_missing(monkeypatch) -> None:
    audio_module._ffplay_supports_audio_device.cache_clear()

    def run(cmd, **kwargs):
        raise FileNotFoundError("ffplay")

    monkeypatch.setattr(audio_module.subprocess, "run", run)
    try:
        assert audio_module._ffplay_supports_audio_device() is False
    finally:
        audio_module._ffplay_supports_audio_device.cache_clear()


def test_play_audio_supports_simple_namespace_probe(monkeypatch, tmp_path) -> None:
    """真实 subprocess.run 返回 CompletedProcess 形状；SimpleNamespace 兼容。"""
    monkeypatch.setattr(audio_module, "_ffplay_supports_audio_device", lambda: True)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(audio_module.subprocess, "run", run)
    result = audio_module.play_audio(str(audio), "")

    assert result.success
    assert calls[0][0] == "ffplay"
