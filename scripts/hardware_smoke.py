#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.modem.client import ModemClient  # noqa: E402
from app.modem.parser import parse_modem_line  # noqa: E402


def play_audio(audio_path: str, audio_device: str) -> int:
    """用系统 ffmpeg 播放音频（复用 app.audio.play_audio）；返回退出码。"""
    from app.audio import play_audio as _play

    return _play(audio_path, audio_device).returncode


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Dial one test call and play a WAV file.")
    parser.add_argument("phone", help="Target phone number supplied by the operator")
    parser.add_argument("wav_path", help="Local WAV file to play after VOICE CALL: BEGIN")
    parser.add_argument("--port", default=settings.modem_port)
    parser.add_argument("--baud", type=int, default=settings.modem_baud)
    parser.add_argument("--audio-device", default=settings.audio_device)
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=settings.call_connect_timeout_seconds,
    )
    parser.add_argument("--hangup-after-play", action="store_true")
    args = parser.parse_args()

    wav_path = Path(args.wav_path)
    if not wav_path.exists():
        print(f"WAV file does not exist: {wav_path}", file=sys.stderr)
        return 2

    connected = False
    with ModemClient(args.port, args.baud) as modem:
        print(f"> ATD{args.phone};")
        modem.dial(args.phone)
        deadline = time.monotonic() + args.connect_timeout
        while time.monotonic() < deadline:
            line = modem.read_line()
            if not line:
                continue
            parsed = parse_modem_line(line)
            print(f"< {line}")
            if parsed.event_type == "voice_call_begin":
                connected = True
                break
            if parsed.event_type in {"voice_call_end", "no_carrier", "busy"}:
                break

        if not connected:
            print("Call did not connect before timeout or release.", file=sys.stderr)
            return 1

        print(f"Playing {wav_path} on {args.audio_device}")
        play_result = play_audio(str(wav_path), args.audio_device)
        print(f"ffmpeg exit code: {play_result}")

        if args.hangup_after_play:
            print("> AT+CHUP")
            modem.hangup()

        for line in modem.iter_lines(8):
            print(f"< {line}")

    return play_result


if __name__ == "__main__":
    raise SystemExit(main())
