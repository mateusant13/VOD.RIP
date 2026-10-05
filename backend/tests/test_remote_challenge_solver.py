"""The remote n-challenge solver grant must not silently disappear.

THE DEFECT THIS PINS. ``remote_components`` is what lets yt-dlp download and
execute the n-challenge solver script, and without it every audio-only itag
(140/251) drops out of the format list, the transcription lane's
``bestaudio/best[vcodec=none][acodec!=none]`` falls through to muxed itag 18,
and a speech recogniser is handed h264 video. That is a silent, expensive
regression: no error, no warning, just ~5x the bytes per job.

The failure mode being guarded against is a REFACTOR, not a config mistake. A
future contributor tidying ``sanitize_ytdlp_opts`` can drop the option, or move
it into a caller that some other module does not use, and nothing fails until
a full 8,311-video lane has requeued itself. So these tests assert the
EFFECTIVE opts handed to a real ``yt_dlp.YoutubeDL`` at the guarded seam — not
that a module-level constant equals a literal. A constant test would keep
passing if the constant stopped being applied.

The security side is asserted too, because the same drop is also a
security-relevant silent change in the OTHER direction: the grant must be
revocable, and revoked means the key is explicitly emptied rather than
inherited from whatever a caller passed in.

Run from backend/: python -m pytest tests/test_remote_challenge_solver.py
"""
from __future__ import annotations

import pytest

from services import ytdlp_guard
from services.ytdlp_guard import (
    EJS_REMOTE_SOLVER_COMPONENTS,
    execute_remote_challenge_solver,
    sanitize_ytdlp_opts,
)

ENV = "VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER"


@pytest.fixture()
def solver_on(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(ytdlp_guard, "_EJS_STATE_REPORTED", False, raising=False)
    return True


@pytest.fixture()
def solver_off(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(ytdlp_guard, "_EJS_STATE_REPORTED", False, raising=False)
    return False


# --- the grant reaches the seam, enabled -------------------------------------

def test_enabled_grant_lands_in_sanitize_opts(solver_on):
    """sanitize_ytdlp_opts is the decision point; the option must be set there."""
    out = sanitize_ytdlp_opts({"extractor_args": {"youtube": {"fetch_pot": ["auto"]}}})
    assert "ejs:github" in out["remote_components"]
    assert out["remote_components"] == list(EJS_REMOTE_SOLVER_COMPONENTS)


def test_enabled_grant_survives_a_caller_that_omits_everything(solver_on):
    """The seam ADDS the grant. It is not inherited from an opt bag."""
    assert sanitize_ytdlp_opts({})["remote_components"] == \
        list(EJS_REMOTE_SOLVER_COMPONENTS)


def test_enabled_grant_is_what_yt_dlp_receives_at_the_seam(solver_on):
    """THE REAL ASSERTION: effective opts at the egress, not a constant.

    Drives ``guarded_youtube_dl`` and captures the dict the real
    ``yt_dlp.YoutubeDL`` is constructed with. If the option stops being
    applied anywhere between sanitize and the constructor, this fails — which
    a test that only read a module constant would not.
    """
    yt_dlp = pytest.importorskip("yt_dlp")
    seen: dict = {}

    class _Fake:
        def extract_info(self, url, download=False):
            return {}

    class _Recorder:
        def __init__(self, params):
            seen.update(params)

        def __enter__(self):
            return _Fake()

        def __exit__(self, *exc):
            return False

    real = yt_dlp.YoutubeDL
    try:
        yt_dlp.YoutubeDL = _Recorder
        with ytdlp_guard.guarded_youtube_dl({}):
            pass
    finally:
        yt_dlp.YoutubeDL = real

    assert "ejs:github" in (seen.get("remote_components") or ())


def test_channel_seam_gets_the_grant_too(solver_on):
    """The channel seam is a second egress; it must not be the un-governed one."""
    yt_dlp = pytest.importorskip("yt_dlp")
    seen: dict = {}

    class _Fake:
        def extract_info(self, url, download=False):
            return {}

    class _Recorder:
        def __init__(self, params):
            seen.update(params)

        def __enter__(self):
            return _Fake()

        def __exit__(self, *exc):
            return False

    real = yt_dlp.YoutubeDL
    try:
        yt_dlp.YoutubeDL = _Recorder
        with ytdlp_guard.guarded_youtube_dl_channel({}):
            pass
    finally:
        yt_dlp.YoutubeDL = real

    assert "ejs:github" in (seen.get("remote_components") or ())


# --- the grant is revocable --------------------------------------------------

def test_unset_means_no_remote_components(solver_off):
    """Default-deny: nothing about remote JS happens unless asked for."""
    assert execute_remote_challenge_solver() is False
    assert sanitize_ytdlp_opts({})["remote_components"] == []


def test_turning_it_off_restores_the_previous_behaviour(solver_off):
    """ONE documented way to turn it off, and it actually reverts.

    Set to 0/false/no/off => the key is explicitly emptied, so the previous
    safe-but-blocked behaviour (no remote fetch, challenge unsolved) returns
    rather than depending on a caller's opts.
    """
    for raw in ("0", "false", "no", "off", "OFF", " False "):
        import os

        os.environ[ENV] = raw
        try:
            assert execute_remote_challenge_solver() is False, raw
            assert sanitize_ytdlp_opts({})["remote_components"] == [], raw
        finally:
            os.environ.pop(ENV, None)


def test_a_caller_cannot_smuggle_the_grant_past_the_seam(solver_off):
    """Disabled means DISABLED, even if a caller passes the component in.

    Without this, the knob would be decorative: any call site could enable
    remote code execution while the process-wide setting reads "off", and the
    operator's one documented revert would not actually revert.
    """
    out = sanitize_ytdlp_opts({"remote_components": ["ejs:github", "ejs:npm"]})
    assert out["remote_components"] == []


# --- the knob is legible and pinned -----------------------------------------

def test_knob_name_states_that_it_executes_remote_code():
    """The env var name must read as what it does, not as an opaque flag.

    A name like VODRIP_YTDLP_OPT_7 gives an operator no way to know they just
    granted remote code execution, so this is a real safety property, not
    cosmetics.
    """
    assert "EXECUTE" in ENV
    assert "REMOTE" in ENV


def test_only_the_pinned_github_source_is_granted(solver_on):
    """One source, one grant.

    ``ejs:npm`` would fetch npm packages at solve time — a SECOND remote source
    with a different trust and supply chain. The grant must not widen silently.
    """
    granted = set(sanitize_ytdlp_opts({})["remote_components"])
    assert granted == set(EJS_REMOTE_SOLVER_COMPONENTS)
    assert "ejs:npm" not in granted


def test_pinned_version_and_hash_are_readable_from_the_installed_ytdlp():
    """'What code ran' must be answerable after the fact.

    The version tag and the sha3-512 that gates the download both come from the
    INSTALLED yt-dlp's vendored manifest — not from us — so the audit trail is
    the package's, not this file's.
    """
    manifest = ytdlp_guard._ejs_vendor_manifest()
    assert manifest.get("version"), "installed yt-dlp exposes no EJS version"
    assert len(manifest.get("lib_min_hash", "")) == 128, (
        "expected a sha3-512 hex digest for yt.solver.lib.min.js"
    )
