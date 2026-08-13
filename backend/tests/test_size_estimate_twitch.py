"""Twitch size estimates: manifest BANDWIDTH (tbr) is the true average byte
rate — used at face value, no peak-shrink, no content-blind floor lift.

Calibrated against live probes (2026-08-13) of real Twitch VODs: for every
variant of VODs 2844204540 / 2837735467 / 2837430888, the usher master's
BANDWIDTH matched sampled CDN segment byte rates within 0.978–1.016x
(e.g. 5871.9 kbps declared vs 5855.2 kbps measured on the repro VOD).
"""

from services.size_estimate import (
    bytes_from_bandwidth_bps,
    estimate_bytes_for_selection,
    size_by_quality_from_formats,
)

# Real formats from the Twitch usher master of the user-reported VOD
# (https://www.twitch.tv/videos/2844204540) as parsed by the GQL fast path —
# height/tbr/fps/vcodec/acodec exactly as yt-dlp reports them too.
TWITCH_FORMATS = [
    {
        "height": 1080, "tbr": 5871.9, "fps": 60.0,
        "vcodec": "avc1.64002A", "acodec": "mp4a.40.2", "ext": "mp4",
        "format_id": "1080p60",
        "url": "https://d1m7jfoe9zdc1j.cloudfront.net/x/chunked/highlight-2844204540.m3u8",
    },
    {
        "height": 720, "tbr": 3423.634, "fps": 60.0,
        "vcodec": "avc1.4D4020", "acodec": "mp4a.40.2", "ext": "mp4",
        "format_id": "720p60",
        "url": "https://d1m7jfoe9zdc1j.cloudfront.net/x/720p60/highlight-2844204540.m3u8",
    },
    {
        "height": 480, "tbr": 1459.998, "fps": 30.0,
        "vcodec": "avc1.4D401F", "acodec": "mp4a.40.2", "ext": "mp4",
        "format_id": "480p",
        "url": "https://d1m7jfoe9zdc1j.cloudfront.net/x/480p30/highlight-2844204540.m3u8",
    },
    {
        "height": 360, "tbr": 747.348, "fps": 30.0,
        "vcodec": "avc1.4D401E", "acodec": "mp4a.40.2", "ext": "mp4",
        "format_id": "360p",
        "url": "https://d1m7jfoe9zdc1j.cloudfront.net/x/360p30/highlight-2844204540.m3u8",
    },
    {
        "height": 160, "tbr": 294.912, "fps": 30.0,
        "vcodec": "avc1.4D400C", "acodec": "mp4a.40.2", "ext": "mp4",
        "format_id": "160p",
        "url": "https://d1m7jfoe9zdc1j.cloudfront.net/x/160p30/highlight-2844204540.m3u8",
    },
]

DUR = 2605.0


def test_twitch_tbr_used_at_face_value_per_variant():
    """Manifest BANDWIDTH is the stream average — estimate must equal
    tbr×duration/8 exactly (no 0.55 peak-shrink, no floor lift)."""
    sizes = size_by_quality_from_formats(TWITCH_FORMATS, DUR)
    for fmt in TWITCH_FORMATS:
        h = fmt["height"]
        label = "1080p60" if h == 1080 else ("720p60" if h == 720 else f"{h}p")
        expected = int(fmt["tbr"] * 1000.0 / 8.0 * DUR)
        assert sizes[label] == expected, (
            f"{label}: got {sizes[label]}, want tbr at face value {expected}"
        )


def test_twitch_estimates_within_band_of_real_sampled_sizes():
    """Synthetic 'real' sizes from measured CDN segment byte rates (within
    0.978–1.016x of declared tbr on live probes) — estimate must land inside
    a 1.25x band."""
    real_kbps = {  # measured avg_kbps per variant, VOD 2844204540
        "1080p60": 5855.24, "720p60": 3411.81, "480p": 1436.40,
        "360p": 747.98, "160p": 301.39,
    }
    sizes = size_by_quality_from_formats(TWITCH_FORMATS, DUR)
    for label, real in real_kbps.items():
        real_bytes = real * 1000.0 / 8.0 * DUR
        ratio = sizes[label] / real_bytes
        assert 0.8 <= ratio <= 1.25, f"{label}: {ratio:.2f}x of real size"


def test_reported_scenario_no_2_7x_overestimate():
    """User report: 240s trim of the 2605s VOD at source quality; the actual
    downloaded file is 170,100,169 bytes (162.2 MB). The estimate must stay
    under 1.5x of the real file (was ~2.7x via legacy fallback/floor)."""
    sizes = size_by_quality_from_formats(TWITCH_FORMATS, DUR)
    est = estimate_bytes_for_selection(
        duration_sec=240.0,
        quality="source",
        size_by_quality=sizes,
        full_duration_sec=DUR,
    )
    real = 170_100_169
    ratio = est / real
    assert ratio < 1.5, f"trim estimate {ratio:.2f}x of real file"
    assert ratio > 0.5, f"trim estimate {ratio:.2f}x of real file (under-estimating)"


def test_bandwidth_bps_taken_at_face_value():
    """bytes_from_bandwidth_bps must not shrink the manifest average."""
    assert bytes_from_bandwidth_bps(5_871_900, DUR) == int(5_871_900 * DUR / 8.0)


def test_low_tier_not_lifted_by_content_blind_floor():
    """160p declared 294.9 kbps — the old floor forced 400 kbps (1.36x over);
    now the real average wins."""
    sizes = size_by_quality_from_formats([TWITCH_FORMATS[-1]], DUR)
    expected = int(294.912 * 1000.0 / 8.0 * DUR)
    assert sizes["160p"] == expected
