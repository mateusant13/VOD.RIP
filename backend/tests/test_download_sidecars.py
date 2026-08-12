from services.download_sidecars import format_chat_txt, format_transcript_txt


def test_format_transcript_srt_like():
    body = format_transcript_txt([
        {"start_sec": 1.0, "end_sec": 3.5, "text": "hello"},
    ])
    assert "00:00:01,000 --> 00:00:03,500" in body
    assert "hello" in body


def test_format_chat_lines():
    body = format_chat_txt([
        {"offset_sec": 75, "username": "bob", "text": "hi"},
    ])
    assert body.strip() == "[01:15] bob: hi"
