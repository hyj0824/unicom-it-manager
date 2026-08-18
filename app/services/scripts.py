from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audio as audio_module
from ..audio import install_wav, script_audio_path
from ..config import Settings
from ..models import CallRecord, CallTask, CallbackPlan, Script
from ..tts.none import NoneTTSProvider


def get_tts_provider(settings: Settings):
    provider_name = settings.tts_provider.lower().strip()
    if provider_name == "none":
        return NoneTTSProvider()
    if provider_name == "edge":
        from ..tts.edge import EdgeTTSProvider

        return EdgeTTSProvider(voice=settings.tts_voice)
    raise RuntimeError(f"Unsupported TTS_PROVIDER: {settings.tts_provider}")


def generate_script_audio(db: Session, script: Script, settings: Settings) -> str:
    """为话术生成规范 WAV 并落库状态；返回给用户的反馈消息。

    规范见 app/audio.py：`data/audio/script-{id}-{正文sha1前12位}{扩展名}`，
    扩展名由 Provider 决定（`.wav` / `.mp3`），不做转码。正文未变时命中同名
    文件（缓存）直接复用，不再调用 Provider；正文变化时生成新文件并原子写
    入。失败时把原因写入 `tts_error`，供话术页展示失败状态。
    """
    provider = get_tts_provider(settings)
    target = script_audio_path(script.id, script.body, ext=provider.output_suffix)
    if target.exists():
        script.tts_status = "generated"
        script.wav_path = str(target)
        script.tts_error = ""
        db.add(script)
        return f"音频已存在（正文未变），无需重新生成：{target.name}"

    try:
        result = provider.generate(script.body)
    except Exception as exc:  # noqa: BLE001 - Provider 异常同样要落库为失败
        script.tts_status = "failed"
        script.tts_error = f"{type(exc).__name__}: {exc}"
        db.add(script)
        return f"音频生成失败：{script.tts_error}"

    if result.success and result.audio_path:
        install_wav(Path(result.audio_path), target)
        script.tts_status = "generated"
        script.wav_path = str(target)
        script.tts_error = ""
        db.add(script)
        return f"音频已生成：{target.name}"

    script.tts_status = "failed"
    script.tts_error = result.error_message or "音频生成失败。"
    db.add(script)
    return f"音频生成失败：{script.tts_error}"


def script_audio_url(script: Script) -> str:
    """话术音频位于 data/audio/ 下时返回试听 URL（/audio/<文件名>），否则空串。

    手动指定的外部路径（如 smoke test 用 WAV）不提供 Web 试听，仍可被
    系统 ffmpeg 播放，避免 Web 路由暴露 data/audio 之外的任意文件。
    """
    if not script.wav_path:
        return ""
    try:
        path = Path(script.wav_path).expanduser().resolve()
        path.relative_to(audio_module.AUDIO_DIR.resolve())
    except (ValueError, OSError):
        return ""
    return "/audio/" + quote(path.name)


def referencing_counts(db: Session, script: Script) -> dict[str, int]:
    """统计引用该话术、导致无法硬删除的业务对象数量。"""

    return {
        "plans": db.scalar(
            select(func.count(CallbackPlan.id)).where(CallbackPlan.script_id == script.id)
        )
        or 0,
        "tasks": db.scalar(
            select(func.count(CallTask.id)).where(CallTask.script_id == script.id)
        )
        or 0,
        "records": db.scalar(
            select(func.count(CallRecord.id)).where(CallRecord.script_id == script.id)
        )
        or 0,
    }
