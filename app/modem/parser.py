from __future__ import annotations

import re
from dataclasses import dataclass


END_WITH_DURATION_RE = re.compile(r"VOICE CALL:\s*END:\s*(\d+)")


@dataclass(frozen=True)
class ParsedModemLine:
    event_type: str
    raw_line: str
    duration_seconds: int | None = None


def parse_modem_line(line: str) -> ParsedModemLine:
    raw = line.strip()
    upper = raw.upper()

    duration_match = END_WITH_DURATION_RE.search(upper)
    if duration_match:
        return ParsedModemLine(
            event_type="voice_call_end",
            raw_line=raw,
            duration_seconds=int(duration_match.group(1)),
        )

    if upper == "VOICE CALL: BEGIN":
        return ParsedModemLine(event_type="voice_call_begin", raw_line=raw)
    if upper == "VOICE CALL: END":
        return ParsedModemLine(event_type="voice_call_end", raw_line=raw)
    if upper == "NO CARRIER":
        return ParsedModemLine(event_type="no_carrier", raw_line=raw)
    if upper == "BUSY":
        return ParsedModemLine(event_type="busy", raw_line=raw)
    if upper.startswith("+CLCC:"):
        return ParsedModemLine(event_type="clcc", raw_line=raw)
    if upper == "OK":
        return ParsedModemLine(event_type="ok", raw_line=raw)
    if upper == "ERROR":
        return ParsedModemLine(event_type="error", raw_line=raw)
    if upper:
        return ParsedModemLine(event_type="urc", raw_line=raw)
    return ParsedModemLine(event_type="empty", raw_line=raw)
