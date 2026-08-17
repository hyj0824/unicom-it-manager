from app.modem.parser import parse_modem_line


def test_parse_voice_call_begin():
    parsed = parse_modem_line("VOICE CALL: BEGIN")
    assert parsed.event_type == "voice_call_begin"


def test_parse_voice_call_end_with_duration():
    parsed = parse_modem_line("VOICE CALL: END: 000008")
    assert parsed.event_type == "voice_call_end"
    assert parsed.duration_seconds == 8


def test_parse_no_carrier():
    parsed = parse_modem_line("NO CARRIER")
    assert parsed.event_type == "no_carrier"


def test_parse_clcc():
    parsed = parse_modem_line('+CLCC: 1,0,0,0,0,"TEST_NUMBER",129,""')
    assert parsed.event_type == "clcc"
