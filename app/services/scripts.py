from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Script
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
