"""Self-check: the muxed-progressive fast branch is reachable for muxed data.

Background: the audit log showed videos where InnerTube returned
muxed progressive formats (muxed=N, dash_https=0) yet the session was
created as kind=hls. The slow canplay path was caused by NOT taking the
muxed-progressive fast branch.

This test asserts:
  C1: _youtube_is_dash_separate_audio correctly classifies muxed data as
      NOT dash-separate (so the muxed-progressive branch can fire).
  C2: _deduped_progressive_variants preserves all muxed heights (the
      quality menu must not collapse to a single tier).
  C3: the muxed-progressive fast branch still exists in source (regression
      net against accidental deletion).

ponytail: live end-to-end e2e pending a stable YouTube muxed-only fixture.
Network-free unit checks cover the data-shape contract that gates the
fast branch.
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import preview_service as ps


def _fake_muxed_formats():
    """Mimic TqYFr_BUZgo: 6 muxed progressive tiers with bundled audio."""
    heights = [144, 240, 360, 480, 720, 1080]
    out = []
    for h in heights:
        out.append({
            "format_id": f"muxed-{h}",
            "url": f"https://rr1.googlevideo.com/videoplayback?itag={h}",
            "height": h,
            "width": int(h * 16 / 9) if h >= 240 else 426,
            "tbr": float(h) / 36,
            "acodec": "mp4a.40.2",
            "vcodec": "avc1.640028" if h >= 720 else "avc1.4d401f",
            "protocol": "https",
            "ext": "mp4",
        })
    return out


def _run():
    muxed_fmts = _fake_muxed_formats()
    yt_info = {
        "id": "TqYFr_BUZgo",
        "_preview_audio_format": {
            "url": "https://rr1.googlevideo.com/videoplayback?audio",
            "acodec": "mp4a.40.2",
        },
        "formats": muxed_fmts,
    }

    # --- C1: muxed data is NOT classified as DASH-separate-audio --------
    #   If this returned True, resolve_stream_info would skip the
    #   muxed-progressive fast branch and fall through to synthetic HLS.
    is_dash_sep = ps._youtube_is_dash_separate_audio(muxed_fmts, yt_info)
    assert is_dash_sep is False, (
        "Bug 5: InnerTube muxed-with-bundled-audio was misclassified as "
        "DASH-separate audio; the muxed-progressive fast branch would be "
        "skipped and the canplay path falls through to slow HLS synthesis."
    )

    # --- C2: dedup preserves all muxed heights ---------------------------
    muxed_progressive = ps._deduped_progressive_variants({"formats": muxed_fmts})
    assert muxed_progressive, (
        "Bug 5: _deduped_progressive_variants dropped all muxed tiers."
    )
    surviving = {
        int(f.get("height") or 0)
        for f in muxed_progressive
        if int(f.get("height") or 0) > 0
    }
    assert len(surviving) >= 6, (
        f"Bug 5: muxed dedup kept only {len(surviving)} heights (expected >=6); "
        f"the quality menu would collapse."
    )

    # --- C3: source-text regression net ----------------------------------
    #   Catches accidental deletion of the fast branch on future refactors.
    src = (ROOT / "services" / "preview_service.py").read_text(encoding="utf-8")
    assert "muxed_progressive" in src, (
        "Bug 5 REGRESSION: 'muxed_progressive' no longer appears in "
        "preview_service.py - the muxed fast branch was deleted."
    )
    assert "not _youtube_is_dash_separate_audio" in src, (
        "Bug 5 REGRESSION: the muxed-progressive guard was removed; the fast "
        "branch no longer only fires on non-DASH-sep-audio sources."
    )

    # --- C4: the muxed_progressive fast path's progression branch exists -
    #   Match the actual decision-arm string in resolve_stream_info.
    assert 'return _yt_resolve("progressive", prog_url' in src, (
        "Bug 5 REGRESSION: _yt_resolve('progressive', ...) fast return was "
        "removed; muxed sources now fall through to HLS."
    )


if __name__ == "__main__":
    try:
        _run()
        print("OK: muxed-progressive fast branch is reachable for InnerTube muxed data")
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
