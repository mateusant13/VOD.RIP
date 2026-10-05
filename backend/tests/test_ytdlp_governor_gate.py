"""yt-dlp egress under the adaptive rate governor.

The defect this pins: yt-dlp is the OTHER YouTube egress and had ZERO governor
call sites. InnerTube is metered at its one /player POST
(youtube_innertube._governor_admit) which is 100% of that traffic, but every
yt-dlp operation — extract, bestaudio download, channel listing, chat display
names — went straight at YouTube, unlearned and ungoverned, and the
transcription path runs straight through them.

What is pinned here:
  1. each chokepoint acquires exactly one token per operation (consistent
     accounting with the governed YouTube path — no parallel scheme);
  2. the gate sits at the operation's ENTRY POINT, not per inner HTTP request
     (the mistake that forced the innertube gate to be moved out of
     _player_request and out of the 2.5s profile race);
  3. an exhausted budget is a BOUNDED wait then a clear, attributable failure —
     never a hang, and never silently proceeding into a wall;
  4. the refusal is worded so no other classifier misreads it as a bot gate, an
     age gate, or a permanent (irreversibly 'blocked') verdict;
  5. HLS segments stay COUNTED-but-not-paced, which is the existing intent.

No network: the yt-dlp context managers are stubbed; the governor is driven
with an injected fake acquire().

Run from backend/: python -m pytest tests/test_ytdlp_governor_gate.py
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from services import archive_ytdlp, rate_budget, ytdlp_guard
from services.archive_ytdlp import YtGovernorExhausted


class _FakeYdl:
    """Minimal stand-in for a YoutubeDL handle."""

    def __init__(self):
        self.extracted: list[tuple[str, bool]] = []

    def extract_info(self, url, download=False):
        self.extracted.append((url, download))
        return {
            "id": "VzuPKrGl0z8",
            "title": "some channel",
            "channel": "some channel",
            "uploader": "some channel",
            "entries": [{"id": "VzuPKrGl0z8", "title": "t", "duration": 60}],
        }


@pytest.fixture()
def acquires(monkeypatch):
    """Record every acquire() and return a scripted decision sequence.

    Returns a small controller: `.calls` is the list of (platform, source, kind)
    triples, and `.script` is a list of Decisions consumed in order (the last
    one repeats once exhausted).
    """
    ctl = types.SimpleNamespace(calls=[], script=[])

    def _acquire(platform, source="auto", *, kind=None):
        ctl.calls.append((platform, source, kind))
        if ctl.script:
            d = ctl.script.pop(0) if len(ctl.script) > 1 else ctl.script[0]
        else:
            d = rate_budget.Decision(
                platform=platform, source=source, allowed=True, wait_s=0.0,
                ceiling_rpm=60.0, tokens=5.0, reason="ok",
            )
        return d

    monkeypatch.setattr(rate_budget, "acquire", _acquire)
    return ctl


def _decision(allowed, wait_s=0.0, source="auto"):
    return rate_budget.Decision(
        platform="youtube", source=source, allowed=allowed, wait_s=wait_s,
        ceiling_rpm=2.0, tokens=-1.0, reason="ok" if allowed else "auto_exhausted",
    )


def test_admit_takes_exactly_one_token_when_allowed(acquires):
    archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_extract")
    assert acquires.calls == [("youtube", "auto", "yt_dlp_extract")]


def test_admit_is_silent_and_sleep_free_when_allowed(acquires, monkeypatch):
    """The common case must add no latency: no sleep, no raise."""
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_bestaudio")
    assert slept == []


def test_exhausted_budget_waits_bounded_then_fails_attributably(acquires, monkeypatch):
    """BOUNDED wait, then a clear failure. Never a hang."""
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    acquires.script = [_decision(False, wait_s=1_000_000.0)]

    with pytest.raises(YtGovernorExhausted) as exc:
        archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_bestaudio")

    # The 1,000,000s the decision asked for is clamped to the bound.
    assert slept and slept[0] <= archive_ytdlp._YTDLP_GOVERNOR_MAX_WAIT_S
    assert slept[0] <= rate_budget.MAX_AUTO_WAIT_S

    msg = str(exc.value)
    assert "yt_dlp_bestaudio" in msg, "must name the operation"
    assert "rate limit" in msg.lower(), "must say it is a rate/budget refusal"
    assert "ceiling=" in msg, "must carry the learned ceiling so it is attributable"
    assert "platform=youtube" in msg and "source=auto" in msg


def test_exhausted_budget_never_waits_unbounded(acquires, monkeypatch):
    """THE anti-hang guarantee, pinned against pathological governor output.

    Steady Watcher can clamp this box's throughput hard, and a governor that
    returns a huge (or non-finite) wait must not turn into a job that sleeps
    for hours. Every shape of wait_s is clamped to _YTDLP_GOVERNOR_MAX_WAIT_S,
    and a USER/interactive caller never sleeps at all. Whatever the governor
    says, the worst case is one bounded sleep and then a clear failure.
    """
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    bound = archive_ytdlp._YTDLP_GOVERNOR_MAX_WAIT_S

    for pathological in (1e12, float("inf"), float("nan"), -5.0, 0.0):
        slept.clear()
        acquires.calls.clear()
        acquires.script = [_decision(False, wait_s=pathological)]
        with pytest.raises(YtGovernorExhausted):
            archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_extract")
        assert len(slept) <= 1, f"wait_s={pathological} produced {len(slept)} sleeps"
        for s in slept:
            assert 0.0 <= s <= bound, f"wait_s={pathological} slept {s}s > {bound}s"

    # And the call always terminated by raising — never returned, never hung.
    assert acquires.calls, "the governor must still be consulted"


def test_user_source_never_waits(acquires, monkeypatch):
    """On-demand work is never queued behind background work."""
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    acquires.script = [_decision(False, wait_s=600.0, source="user")]

    with pytest.raises(YtGovernorExhausted):
        archive_ytdlp._governor_admit_ytdlp("user", "yt_dlp_extract", interactive=True)
    assert slept == [], "a USER caller must fail fast, not sleep"


def test_interactive_flag_short_circuits_the_wait(acquires, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: slept.append(s))
    acquires.script = [_decision(False, wait_s=600.0, source="auto")]
    with pytest.raises(YtGovernorExhausted):
        archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_extract", interactive=True)
    assert slept == []


def test_missing_governor_module_is_not_a_new_failure_mode(monkeypatch):
    """A broken/absent rate_budget must not become a new failure path."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "services.rate_budget":
            raise ImportError("no governor")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    # Must not raise: yt-dlp egress proceeds ungated rather than dying.
    archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_extract")


# --- the chokepoints actually call it ----------------------------------------

def test_extract_chokepoint_governed(acquires, monkeypatch):
    """_guarded_youtube_dl is the extract/chat-backfill seam (ingest_video and
    backfill_live_chat both go through it) — one token per extract."""
    ydl = _FakeYdl()
    monkeypatch.setattr(archive_ytdlp, "_yt_opts", lambda outdir, video_id=None: {})
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl", lambda opts, **_control: _ctx(ydl))
    with archive_ytdlp._guarded_youtube_dl(Path("."), video_id="VzuPKrGl0z8") as y:
        y.extract_info("https://youtu.be/VzuPKrGl0z8", download=False)
    assert acquires.calls == [("youtube", "auto", "yt_dlp_extract")]
    assert len(ydl.extracted) == 1


def test_bestaudio_chokepoint_governed(acquires, monkeypatch, tmp_path):
    """The transcription path's own egress. Paced once per DOWNLOAD, never per
    inner segment request — a resume re-reads hundreds of chunks inside
    extract_info and pacing those would hold the download hostage.

    NOTE the seam: download_bestaudio used to re-import guarded_youtube_dl INSIDE
    the function, which shadowed the module attribute — a patch on
    archive_ytdlp missed it and the REAL yt-dlp ran, a live network call in a
    unit test. The name now resolves through the module import
    (`ytdlp_guard.guarded_youtube_dl`), so this ONE patch is the seam for every
    guard call in the process. tests/test_guard_binding_seam.py fails if a
    function-local re-import comes back.
    """
    ydl = _FakeYdl()
    monkeypatch.setattr(archive_ytdlp, "_audio_resume_dir", lambda vid: tmp_path)
    monkeypatch.setattr(archive_ytdlp, "_apply_youtube_session", lambda *a, **kw: None)
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl", lambda opts, **_control: _ctx(ydl))
    with pytest.raises(Exception):
        # No media file is produced by the stub, so the post-download lookup
        # fails — the point is that the governor ran exactly once first.
        archive_ytdlp.download_bestaudio("VzuPKrGl0z8", tmp_path)
    assert acquires.calls == [("youtube", "auto", "yt_dlp_bestaudio")]
    assert len(ydl.extracted) == 1, "yt-dlp itself must still be reached"


def test_bestaudio_refuses_before_touching_the_network(acquires, monkeypatch, tmp_path):
    """Ordering matters: a dry pool must be refused at the entry point, so no
    request is made at all (the whole point of gating here, not per request)."""
    ydl = _FakeYdl()
    monkeypatch.setattr(archive_ytdlp, "_audio_resume_dir", lambda vid: tmp_path)
    monkeypatch.setattr(archive_ytdlp, "_apply_youtube_session", lambda *a, **kw: None)
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl", lambda opts, **_control: _ctx(ydl))
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.script = [_decision(False, wait_s=1.0)]

    with pytest.raises(YtGovernorExhausted):
        archive_ytdlp.download_bestaudio("VzuPKrGl0z8", tmp_path)
    assert ydl.extracted == [], "the governor gate must run BEFORE any yt-dlp call"


def test_channel_list_chokepoint_governed(acquires, monkeypatch):
    ydl = _FakeYdl()
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts, **_control: _ctx(ydl))
    out = archive_ytdlp.list_channel_videos("https://youtube.com/@chan", limit=3)
    assert [e["id"] for e in out] == ["VzuPKrGl0z8"]
    # One token per channel-tab walk — extract_flat makes it ONE listing
    # request, not one per entry.
    assert acquires.calls == [("youtube", "auto", "yt_dlp_channel_list")]


def test_display_name_backfill_governed_per_channel(acquires, monkeypatch):
    """A whole batch of distinct channel ids per run — the highest-frequency
    ungoverned YouTube egress in this module."""
    ydl = _FakeYdl()
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts, **_control: _ctx(ydl))
    monkeypatch.setattr(
        archive_ytdlp.archive_db, "youtube_chat_user_ids_without_display_name",
        lambda limit: ["UCaaa", "UCbbb"],
    )
    resolved = archive_ytdlp.resolve_youtube_display_names(limit=2)
    assert resolved == 2
    assert [c[2] for c in acquires.calls] == ["yt_dlp_channel_meta"] * 2


def test_display_name_backfill_stops_the_batch_when_the_pool_is_dry(acquires, monkeypatch):
    """A dry pool must not pay the bounded wait once PER REMAINING ID — that
    would turn a 20-id batch into a 20x stall. It stops, and the unresolved ids
    are picked up on a later run (the existing retry contract)."""
    ydl = _FakeYdl()
    monkeypatch.setattr(ytdlp_guard, "guarded_youtube_dl_channel", lambda opts, **_control: _ctx(ydl))
    monkeypatch.setattr(
        archive_ytdlp.archive_db, "youtube_chat_user_ids_without_display_name",
        lambda limit: ["UCaaa", "UCbbb", "UCccc"],
    )
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    acquires.script = [_decision(False, wait_s=1.0)]

    resolved = archive_ytdlp.resolve_youtube_display_names(limit=3)
    assert resolved == 0
    # ONE admission attempt, not three: the batch bailed on the first refusal.
    assert len(acquires.calls) == 1


def test_governor_refusal_never_reads_as_a_gate_or_a_permanent_verdict(acquires, monkeypatch):
    """The refusal text is load-bearing: ingest_video classifies it, and
    archive_transcribe decides 'blocked' (IRREVERSIBLE) from it. A dry pool is
    neither a bot wall nor a dead video, and must never poison a video row or
    write a bogus rate-limit event.
    """
    from services.yt_gate import classify_youtube_gate_error
    from services.youtube_diag import is_age_gate_error, is_age_gate_job_error

    acquires.script = [_decision(False, wait_s=1.0)]
    monkeypatch.setattr(archive_ytdlp.time, "sleep", lambda s: None)
    with pytest.raises(YtGovernorExhausted) as exc:
        archive_ytdlp._governor_admit_ytdlp("auto", "yt_dlp_extract")
    exc_obj = exc.value
    msg = str(exc_obj)

    assert not classify_youtube_gate_error(exc_obj), msg
    assert not archive_ytdlp._is_gate_error(exc_obj), msg
    assert not is_age_gate_error(exc_obj), msg
    assert not archive_ytdlp._is_permanent_download_error(exc_obj), msg
    assert not is_age_gate_job_error(msg), msg
    # Not terminal in archive_db.update_job either, and rate-classified so the
    # job requeues instead of burning max_attempts on a transient refusal.
    assert "ASR unsupported" not in msg and "FileNotFound" not in msg
    assert "rate limit" in msg.lower()


def test_hls_segments_stay_counted_not_paced():
    """HLS is deliberately counter-only (note_hot_call), NOT token-paced: it
    sits in the 12-thread download hot path where thousands of paced calls
    would cost more than the pacing is worth. Pinned so the yt-dlp governance
    work does not quietly change that intent."""
    import inspect

    from services import ytdlp_hls

    src = inspect.getsource(ytdlp_hls._note_segment_call)
    assert "note_hot_call" in src
    assert "acquire" not in src, "HLS segments must not become token-paced"
    assert "hls_segment" in src


def _ctx(ydl):
    import contextlib

    @contextlib.contextmanager
    def _cm():
        yield ydl

    return _cm()
