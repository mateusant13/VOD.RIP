"""Self-check for live-stream detection / DVR helpers.

Run from repo root with:
    python -m backend.tests.test_live_capture
or from the backend directory with:
    python -m tests.test_live_capture
"""
from services.live_capture import _emit_progress
from routers import live


def test_progress_hook_shape() -> None:
    calls: list[dict] = []

    def _hook(d: dict) -> None:
        calls.append(d)

    _emit_progress(b"size=123kB time=00:01:23.45 bitrate=...", _hook)
    assert len(calls) == 1, "progress hook should be called once for a time= line"
    d = calls[0]
    assert d.get("status") == "downloading"
    assert d.get("percent") == 0
    assert d.get("speed") == "live 83.45s"
    assert d.get("eta_seconds") is None


def test_live_router_imports() -> None:
    # Smoke test that the router module loads without import errors.
    assert hasattr(live, "router")
    assert hasattr(live, "check_live_status")
    assert hasattr(live, "channel_live_status")


if __name__ == "__main__":
    test_progress_hook_shape()
    test_live_router_imports()
    print("live_capture self-check OK")
