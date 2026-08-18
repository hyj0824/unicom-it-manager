from __future__ import annotations

"""P2 话术音频：存储规范、生成失败状态、重生成入口与页面试听。

覆盖：
- `script_audio_path` 目录/命名/正文哈希（正文不变同名缓存，正文变化新文件）；
- 原子写入与覆盖（`write_wav_atomic` / `install_wav`），无残留临时文件；
- `resolve_audio_file` 与 `/audio/{filename}` 只读路由：登录保护、路径穿越、
  symlink 穿透与非 WAV 拒绝；
- `generate_script_audio`：TTS_PROVIDER=none 默认失败路径落库 `tts_error`，
  成功路径写规范路径并缓存命中不重复调用 Provider，Provider 异常归为失败；
- 话术页：失败状态展示、重生成入口结果反馈、试听按钮仅对 data/audio 内
  的 WAV 渲染。

不接触真实串口与 ffplay；TestClient 不用上下文管理器，避免启动
Scheduler / Call Worker 线程。音频写入通过 monkeypatch 重定向到临时目录。
"""

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.audio as audio_module
from app.config import Settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import Script
from app.services import scripts as script_service
from app.tts.base import TTSResult

BASE_DIR = Path(__file__).resolve().parent.parent
DB_URL = os.environ["DATABASE_URL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]


# ---------------------------------------------------------------- 基础设施


def _reset_db() -> None:
    engine.dispose()
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    _reset_db()
    # 音频写入重定向到临时目录，不触碰仓库 data/audio。
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    # 不用 `with`：lifespan 不执行，调度器与 Worker 线程不会启动。
    return TestClient(app)


@pytest.fixture()
def db(monkeypatch, tmp_path):
    _reset_db()
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        admin_password="test", session_secret="test", database_url="sqlite:///:memory:",
        modem_port="/dev/ttyUSB-DOES-NOT-EXIST", modem_baud=115200,
        audio_device="plughw:0,0", call_connect_timeout_seconds=90,
        rejected_end_seconds=20, min_connected_seconds=8, retry_delay_seconds=300,
        max_call_attempts=2, tts_provider="none", tts_api_key="", tts_voice="",
        default_timezone="Asia/Shanghai", call_worker_enabled=False, worker_poll_seconds=5,
    )


def login(client: TestClient, password: str = ADMIN_PASSWORD) -> None:
    resp = client.post(
        "/login", data={"username": "admin", "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


class _FakeTTSProvider:
    """成功 Provider：每次调用写一个音频文件并记录调用次数。"""

    output_suffix = ".wav"

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.calls = 0

    def generate(self, text: str) -> TTSResult:
        self.calls += 1
        out = self.out_dir / f"provider-{self.calls}{self.output_suffix}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF-fake-wave")
        return TTSResult(success=True, audio_path=str(out))


class _FailingTTSProvider:
    output_suffix = ".wav"

    def generate(self, text: str) -> TTSResult:
        raise RuntimeError("provider exploded")


def _script(db, title: str = "话术A", body: str = "正文内容") -> Script:
    script = Script(title=title, body=body)
    db.add(script)
    db.commit()
    db.refresh(script)
    return script


def _create_script_via_web(client: TestClient, **data) -> int:
    payload = {"title": "话术A", "body": "正文内容", "wav_path": ""}
    payload.update(data)
    resp = client.post("/scripts", data=payload, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    with SessionLocal() as session:
        return session.scalars(select(Script).order_by(Script.id.desc())).first().id


# ---------------------------------------------------------------- 存储规范：目录 / 命名 / 覆盖


def test_script_audio_path_naming_and_content_hash() -> None:
    p1 = audio_module.script_audio_path(3, "您好，这里是联通。")
    p2 = audio_module.script_audio_path(3, "您好，这里是联通。")
    p3 = audio_module.script_audio_path(3, "正文不同")
    p4 = audio_module.script_audio_path(7, "您好，这里是联通。")
    assert p1 == p2  # 正文不变 → 同名（缓存）
    assert p1.name != p3.name  # 正文变化 → 新文件名
    assert p1.name != p4.name  # 话术 id 参与命名
    assert re.fullmatch(r"script-\d+-[0-9a-f]{12}\.wav", p1.name)
    assert p1.parent == audio_module.AUDIO_DIR  # 位于 data/audio/
    # 扩展名由 Provider 决定（edge-tts 输出 .mp3）。
    mp3 = audio_module.script_audio_path(3, "您好，这里是联通。", ext=".mp3")
    assert re.fullmatch(r"script-\d+-[0-9a-f]{12}\.mp3", mp3.name)
    assert mp3.with_suffix("") == p1.with_suffix("")  # 同一正文同一基础名


def test_write_wav_atomic_writes_and_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "a.wav"
    audio_module.write_wav_atomic(target, b"first")
    assert target.read_bytes() == b"first"
    # 重新生成：原子覆盖旧内容。
    audio_module.write_wav_atomic(target, b"second-overwrite")
    assert target.read_bytes() == b"second-overwrite"
    # 无残留临时文件。
    assert list(tmp_path.iterdir()) == [target]


def test_install_wav_copies_content_atomically(tmp_path: Path) -> None:
    source = tmp_path / "provider-out.wav"
    source.write_bytes(b"RIFF-data")
    target = tmp_path / "sub" / "dst.wav"
    audio_module.install_wav(source, target)
    assert target.read_bytes() == b"RIFF-data"
    assert source.read_bytes() == b"RIFF-data"  # 源文件保留


# ---------------------------------------------------------------- 只读音频路由的路径防护


def test_resolve_audio_file_serves_only_audio_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    wav = tmp_path / "script-1-abcdef123456.wav"
    wav.write_bytes(b"RIFF")
    mp3 = tmp_path / "script-1-abcdef123456.mp3"
    mp3.write_bytes(b"ID3")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.wav").write_bytes(b"RIFF2")
    (tmp_path / "secret.txt").write_text("no")

    assert audio_module.resolve_audio_file("script-1-abcdef123456.wav") == wav
    assert audio_module.resolve_audio_file("script-1-abcdef123456.mp3") == mp3
    assert audio_module.resolve_audio_file("sub/nested.wav") is not None  # 允许子目录
    # 越界与非法输入一律拒绝。
    assert audio_module.resolve_audio_file("../secret.txt") is None
    assert audio_module.resolve_audio_file("..") is None
    assert audio_module.resolve_audio_file("/etc/passwd") is None
    assert audio_module.resolve_audio_file("a/../../secret.txt") is None
    assert audio_module.resolve_audio_file("secret.txt") is None  # 非音频扩展名
    assert audio_module.resolve_audio_file("missing.wav") is None
    assert audio_module.resolve_audio_file("missing.mp3") is None
    assert audio_module.resolve_audio_file("") is None

    # symlink 指向目录外 → resolve 后越界，拒绝。
    outside = tmp_path.parent / "outside.wav"
    outside.write_bytes(b"x")
    (tmp_path / "evil.wav").symlink_to(outside)
    assert audio_module.resolve_audio_file("evil.wav") is None


def test_script_audio_url_only_for_audio_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    managed = Script(title="t", body="b", wav_path=str(tmp_path / "script-1-abcdef123456.wav"))
    assert script_service.script_audio_url(managed) == "/audio/script-1-abcdef123456.wav"
    external = Script(title="t", body="b", wav_path="/home/radxa/audio/callback.wav")
    assert script_service.script_audio_url(external) == ""
    empty = Script(title="t", body="b")
    assert script_service.script_audio_url(empty) == ""


def test_generate_audio_mp3_provider_installs_mp3_and_previews(client, tmp_path, monkeypatch) -> None:
    """edge 风格 Provider（输出 .mp3）：落盘 .mp3、试听路由返回 audio/mpeg。"""
    login(client)
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    provider = _FakeTTSProvider(tmp_path / "provider-out")
    provider.output_suffix = ".mp3"
    monkeypatch.setattr(script_service, "get_tts_provider", lambda s: provider)
    script_id = _create_script_via_web(client)

    resp = client.post(f"/scripts/{script_id}/generate-audio", follow_redirects=True)
    assert resp.status_code == 200
    assert "音频已生成" in resp.text

    with SessionLocal() as session:
        script = session.get(Script, script_id)
        assert script.tts_status == "generated"
        name = Path(script.wav_path).name
        assert name.endswith(".mp3")

    assert f"/audio/{name}" in resp.text
    audio = client.get(f"/audio/{name}")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert audio.content == b"RIFF-fake-wave"

    # 正文未变 → 缓存命中（同样按 .mp3 后缀查找）。
    resp2 = client.post(f"/scripts/{script_id}/generate-audio", follow_redirects=True)
    assert "无需重新生成" in resp2.text
    assert provider.calls == 1


# ---------------------------------------------------------------- 生成服务：成功 / 失败 / 缓存


def test_generate_audio_none_provider_marks_failed(db, settings) -> None:
    script = _script(db)
    message = script_service.generate_script_audio(db, script, settings)
    db.commit()

    assert script.tts_status == "failed"
    assert "TTS_PROVIDER=none does not generate audio." in script.tts_error
    assert script.wav_path == ""
    assert "音频生成失败" in message


def test_generate_audio_success_installs_atomic_path_and_caches(db, settings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    script = _script(db, body="您好，回访测试。")
    provider = _FakeTTSProvider(tmp_path / "provider-out")
    monkeypatch.setattr(script_service, "get_tts_provider", lambda s: provider)

    message = script_service.generate_script_audio(db, script, settings)
    db.commit()
    target = audio_module.script_audio_path(script.id, script.body)
    assert message == f"音频已生成：{target.name}"
    assert script.tts_status == "generated"
    assert script.tts_error == ""
    assert script.wav_path == str(target)
    assert target.read_bytes() == b"RIFF-fake-wave"
    assert provider.calls == 1

    # 正文未变 → 缓存命中，不重复调用 Provider，状态重新置为 generated。
    script.tts_status = "not_generated"
    message2 = script_service.generate_script_audio(db, script, settings)
    db.commit()
    assert "无需重新生成" in message2
    assert provider.calls == 1
    assert script.tts_status == "generated"

    # 正文变化 → 新文件名原子写入，旧文件保留为历史缓存。
    script.body = "正文已修改"
    db.commit()
    message3 = script_service.generate_script_audio(db, script, settings)
    db.commit()
    assert provider.calls == 2
    assert Path(script.wav_path).name != target.name
    assert target.exists()


def test_generate_audio_provider_exception_marks_failed(db, settings, monkeypatch) -> None:
    script = _script(db)
    monkeypatch.setattr(script_service, "get_tts_provider", lambda s: _FailingTTSProvider())
    message = script_service.generate_script_audio(db, script, settings)
    db.commit()

    assert script.tts_status == "failed"
    assert "provider exploded" in script.tts_error
    assert script.wav_path == ""
    assert "音频生成失败" in message


# ---------------------------------------------------------------- Web：失败状态、重生成入口、试听


def test_generate_audio_failure_shows_status_and_feedback(client) -> None:
    login(client)
    script_id = _create_script_via_web(client)

    resp = client.post(f"/scripts/{script_id}/generate-audio", follow_redirects=True)
    assert resp.status_code == 200
    assert "音频生成失败" in resp.text
    assert "TTS_PROVIDER=none does not generate audio." in resp.text  # 失败原因
    assert "生成音频" in resp.text  # 重生成入口仍在

    with SessionLocal() as session:
        script = session.get(Script, script_id)
        assert script.tts_status == "failed"
        assert "does not generate audio" in script.tts_error
        assert script.wav_path == ""


def test_generate_audio_success_via_web_and_preview_route(client, tmp_path, monkeypatch) -> None:
    login(client)
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    provider = _FakeTTSProvider(tmp_path / "provider-out")
    monkeypatch.setattr(script_service, "get_tts_provider", lambda s: provider)
    script_id = _create_script_via_web(client)

    resp = client.post(f"/scripts/{script_id}/generate-audio", follow_redirects=True)
    assert resp.status_code == 200
    assert "音频已生成" in resp.text

    with SessionLocal() as session:
        script = session.get(Script, script_id)
        assert script.tts_status == "generated"
        assert script.tts_error == ""
        name = Path(script.wav_path).name

    # 页面试听：<audio> 元素指向只读音频路由，内容与落盘一致。
    assert f"/audio/{name}" in resp.text
    audio = client.get(f"/audio/{name}")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.content == b"RIFF-fake-wave"


def test_audio_route_requires_login(client) -> None:
    resp = client.get("/audio/anything.wav", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_audio_route_rejects_traversal_and_non_wav(client, tmp_path, monkeypatch) -> None:
    login(client)
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    (tmp_path / "ok.wav").write_bytes(b"RIFF")
    (tmp_path / "ok.mp3").write_bytes(b"ID3-fake-mp3")
    (tmp_path / "secret.txt").write_text("top secret")

    for bad in [
        "../secret.txt",
        "%2E%2E/secret.txt",
        "%2Fetc%2Fpasswd",
        "ok.txt",
        "missing.wav",
        "missing.mp3",
    ]:
        resp = client.get(f"/audio/{bad}")
        assert resp.status_code == 404, bad

    good = client.get("/audio/ok.wav")
    assert good.status_code == 200
    assert good.content == b"RIFF"
    mp3 = client.get("/audio/ok.mp3")
    assert mp3.status_code == 200
    assert mp3.headers["content-type"].startswith("audio/mpeg")
    assert mp3.content == b"ID3-fake-mp3"


def test_scripts_page_preview_only_for_managed_audio(client, tmp_path, monkeypatch) -> None:
    login(client)
    monkeypatch.setattr(audio_module, "AUDIO_DIR", tmp_path)
    managed = tmp_path / "script-9-abcdef123456.wav"
    managed.write_bytes(b"RIFF")
    _create_script_via_web(client, title="托管音频", wav_path=str(managed))
    _create_script_via_web(client, title="外部音频", wav_path="/home/radxa/audio/callback.wav")
    _create_script_via_web(client, title="无音频", wav_path="")

    page = client.get("/scripts")
    assert page.status_code == 200
    assert "/audio/script-9-abcdef123456.wav" in page.text
    assert page.text.count("<audio") == 1  # 外部路径与空路径不渲染试听
    assert "外部音频" in page.text


def test_script_create_with_wav_path_marks_generated(client) -> None:
    login(client)
    _create_script_via_web(client, title="带路径", wav_path="/tmp/x.wav")
    with SessionLocal() as session:
        script = session.scalars(select(Script)).one()
        assert script.tts_status == "generated"
        assert script.tts_error == ""
