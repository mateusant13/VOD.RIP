"""Self-check: _extract_youtube_preview_info must not raise NameError.

Exercises the exact failing line (the `if vid and not warm_light and not oauth:`
guard) and confirms the function proceeds past it. Mocks heavy network
dependencies so this runs without yt-dlp / network.

ponytail: replaces an integration test; upgrade to a live YouTube watch URL
when a stable CI fixture exists. Lazy imports inside _extract_youtube_preview_info
are patched via ``__init__`` injection on the ``services.youtube_innertube`` and
``services.ytdlp_hls`` modules (NOT mock.patch.object on the function's own
namespace -- the symbols aren't module-level attributes).
"""
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import preview_service as ps
from services import youtube_innertube, ytdlp_hls


def _run() -> None:
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # Path A: warm_light=True, oauth=None -> skip anonymous branch, hit yt-dlp stub.
    with mock.patch.object(
        ytdlp_hls, "cached_extract_info", return_value={"id": "x", "formats": []}
    ):
        info = ps._extract_youtube_preview_info(url, oauth=None, warm_light=True)
    assert isinstance(info, dict), info

    # Path B: vid must be defined. Force anonymous helper to fail (no network)
    # and the cookie cache lookup to return None so we hit the RuntimeError
    # guard -- NOT NameError.
    with mock.patch.object(
        youtube_innertube,
        "innertube_extract_360p_anonymous",
        side_effect=RuntimeError("offline"),
    ), mock.patch.object(
        ytdlp_hls, "cached_extract_info", return_value={"id": "x", "formats": []}
    ):
        try:
            ps._extract_youtube_preview_info(url, oauth=None, warm_light=False)
        except RuntimeError as e:
            assert "Preview unavailable" in str(e), e
        except NameError as e:
            raise AssertionError(f"REGRESSION: NameError still raised: {e}")


if __name__ == "__main__":
    _run()
    print("OK: _extract_youtube_preview_info no longer raises NameError on 'vid'")
