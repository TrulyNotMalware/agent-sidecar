"""Fast checks of the harness' own SSE parsing (no server; runs without -m integration)."""

from .harness import parse_sse_frames


def test_parses_crlf_frames_and_keeps_partial_remainder():
    buf = 'event: session\r\ndata: {"sessionId": "s"}\r\n\r\nevent: text\r\ndata: {"de'

    events, rest = parse_sse_frames(buf)

    assert events == [("session", {"sessionId": "s"})]
    assert rest == 'event: text\ndata: {"de'


def test_frame_split_across_reads_is_parsed_once_complete():
    events, rest = parse_sse_frames('event: done\r\ndata: {"finalText": "x"}\r')
    assert events == []

    events, rest = parse_sse_frames(rest + "\n\r\n")

    assert events == [("done", {"finalText": "x"})]
    assert rest == ""


def test_ping_comment_frames_are_skipped():
    buf = ': ping - 2026-01-01 00:00:00\r\n\r\nevent: done\r\ndata: {}\r\n\r\n'

    events, _ = parse_sse_frames(buf)

    assert events == [("done", {})]
