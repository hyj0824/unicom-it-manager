from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import CallRecord, CallTask, CallbackPlan, Script
from ..tts.none import NoneTTSProvider


def get_tts_provider(settings: Settings):
    provider_name = settings.tts_provider.lower().strip()
    if provider_name == "none":
        return NoneTTSProvider()
    raise RuntimeError(f"Unsupported TTS_PROVIDER: {settings.tts_provider}")


def generate_script_audio(db: Session, script: Script, settings: Settings) -> None:
    provider = get_tts_provider(settings)
    result = provider.generate(script.body)
    if result.success and result.wav_path:
        script.tts_status = "generated"
        script.wav_path = result.wav_path
    else:
        script.tts_status = "failed"
    db.add(script)


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
