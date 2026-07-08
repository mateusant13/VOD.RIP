"""Unit tests for MPEG-TS EXTINF audit (ffprobe mocked)."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.ytdlp_ffmpeg import (
    SEGMENT_EXTINF_TOLERANCE_SEC,
    SegmentAudit,
    audit_segment_extinf,
    probe_ts_duration,
)


def test_audit_segment_extinf_passes_within_tolerance(monkeypatch):
    monkeypatch.setattr(
        "services.ytdlp_ffmpeg.probe_ts_duration",
        lambda *_a, **_k: 9.92,
    )
    monkeypatch.setattr(
        "services.ytdlp_ffmpeg.probe_ts_pts_bounds",
        lambda *_a, **_k: (0.0, 9.90),
    )
    audit = audit_segment_extinf(Path("dummy.ts"), 10.0)
    assert audit.ok
    assert audit.delta == pytest.approx(-0.08)
    assert audit.pts_monotonic is True


def test_audit_segment_extinf_fails_when_truncated(monkeypatch):
    monkeypatch.setattr(
        "services.ytdlp_ffmpeg.probe_ts_duration",
        lambda *_a, **_k: 9.12,
    )
    monkeypatch.setattr(
        "services.ytdlp_ffmpeg.probe_ts_pts_bounds",
        lambda *_a, **_k: (0.0, 9.10),
    )
    audit = audit_segment_extinf(Path("dummy.ts"), 10.0)
    assert not audit.ok
    assert audit.actual_duration == pytest.approx(9.12)


def test_audit_segment_extinf_fails_on_decreasing_pts(monkeypatch):
    monkeypatch.setattr(
        "services.ytdlp_ffmpeg.probe_ts_duration",
        lambda *_a, **_k: 10.0,
    )
    monkeypatch.setattr(
        "services.ytdlp_ffmpeg.probe_ts_pts_bounds",
        lambda *_a, **_k: (5.0, 1.0),
    )
    audit = audit_segment_extinf(Path("dummy.ts"), 10.0)
    assert not audit.ok
    assert audit.pts_monotonic is False


def test_segment_audit_dataclass_ok_flag():
    assert SegmentAudit(10.0, 9.95, -0.05, True).ok
    assert SEGMENT_EXTINF_TOLERANCE_SEC == 0.15


def test_probe_ts_duration_parses_ffprobe_stdout(monkeypatch, tmp_path):
    seg = tmp_path / "seg.ts"
    seg.write_bytes(b"\x00" * 100_000)

    class _Result:
        returncode = 0
        stdout = "9.500000\n"
        stderr = ""

    monkeypatch.setattr(
        "services.ytdlp_ffmpeg._resolve_ffprobe_exe",
        lambda *_a: "ffprobe",
    )
    monkeypatch.setattr("services.ytdlp_ffmpeg.sp.run", lambda *_a, **_k: _Result())
    assert probe_ts_duration(seg) == pytest.approx(9.5)
