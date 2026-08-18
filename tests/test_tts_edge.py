from __future__ import annotations

"""edge-tts Provider 单元测试：mock `edge_tts.Communicate`，不联网。

覆盖：默认/配置发音人、成功产出 MP3、空正文、网络异常、超时与临时文件清理。
"""

import asyncio
from pathlib import Path

import pytest

import app.tts.edge as edge_module
from app.tts.edge import EdgeTTSProvider


class FakeCommunicate:
    """记录构造参数；save() 可配置为成功写文件、抛异常或挂起。"""

    calls: list[tuple[str, str, dict]] = []

    def __init__(self, text: str, voice: str, **kwargs) -> None:
        self.text = text
        self.voice = voice
        self.kwargs = kwargs
        FakeCommunicate.calls.append((text, voice, kwargs))

    async def save(self, path: str) -> None:
        Path(path).write_bytes(b"ID3-fake-mp3")


class FailingCommunicate(FakeCommunicate):
    async def save(self, path: str) -> None:
        raise RuntimeError("network down")


class HangingCommunicate(FakeCommunicate):
    async def save(self, path: str) -> None:
        await asyncio.sleep(30)


@pytest.fixture(autouse=True)
def patch_communicate(monkeypatch):
    FakeCommunicate.calls.clear()
    monkeypatch.setattr(edge_module, "Communicate", FakeCommunicate)


def test_generate_success_writes_mp3_and_uses_default_voice() -> None:
    provider = EdgeTTSProvider()
    result = provider.generate("您好，这里是联通回访。")

    assert result.success
    assert result.audio_path
    path = Path(result.audio_path)
    assert path.exists()
    assert path.suffix == ".mp3"
    assert path.read_bytes() == b"ID3-fake-mp3"
    assert FakeCommunicate.calls == [
        ("您好，这里是联通回访。", "zh-CN-XiaoxiaoNeural", {})
    ]


def test_generate_uses_configured_voice() -> None:
    provider = EdgeTTSProvider(voice="zh-CN-YunxiNeural", rate="+10%")
    result = provider.generate("测试")

    assert result.success
    assert FakeCommunicate.calls[0][1] == "zh-CN-YunxiNeural"
    assert FakeCommunicate.calls[0][2] == {"rate": "+10%"}


def test_generate_empty_text_fails_without_calling() -> None:
    provider = EdgeTTSProvider()
    result = provider.generate("   ")

    assert not result.success
    assert "正文为空" in result.error_message
    assert FakeCommunicate.calls == []


def test_generate_network_error_returns_failure(monkeypatch) -> None:
    monkeypatch.setattr(edge_module, "Communicate", FailingCommunicate)
    provider = EdgeTTSProvider()
    result = provider.generate("测试")

    assert not result.success
    assert "network down" in result.error_message
    assert not Path(result.audio_path or "/nonexistent").exists()  # 临时文件已清理


def test_generate_timeout_returns_failure(monkeypatch) -> None:
    monkeypatch.setattr(edge_module, "Communicate", HangingCommunicate)
    provider = EdgeTTSProvider()
    # 缩短超时，让挂起的 save() 快速超时。
    provider.GENERATE_TIMEOUT_SECONDS = 0.1
    result = provider.generate("测试")

    assert not result.success
    assert "TimeoutError" in result.error_message


def test_provider_output_suffix_is_mp3() -> None:
    assert EdgeTTSProvider.output_suffix == ".mp3"
