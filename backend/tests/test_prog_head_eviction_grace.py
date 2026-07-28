"""Self-check: budget enforcement skips files newer than grace window.

Proves the click\u2192purge race fix: a file written <_PROG_HEAD_MIN_EVICT_AGE_SEC
ago survives purge even when over the byte budget.

Run from anywhere: `python backend/tests/test_prog_head_eviction_grace.py`
"""
import os
import sys
import time

_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from services import preview_service
from services.preview_service import (
    _PROG_HEAD_DIR,
    _PROG_HEAD_MIN_EVICT_AGE_SEC,
)

# Spec: grace MUST exceed typical click-to-play latency so a user clicking
# during a wave-completion purge always reaches the cache.
_GRACE_FLOOR_SEC = 5


def main() -> int:
    """Returns 0 on success, 1 on failure."""
    assert _PROG_HEAD_MIN_EVICT_AGE_SEC > _GRACE_FLOOR_SEC, (
        f"grace {_PROG_HEAD_MIN_EVICT_AGE_SEC}s too short \u2014 click-to-play latency (~{_GRACE_FLOOR_SEC}s) would race"
    )

    sandbox = _PROG_HEAD_DIR / "_eviction_grace_test"
    sandbox.mkdir(parents=True, exist_ok=True)
    try:
        # Two files: fresh (1MB, mtime=now) and old (3MB, mtime far past).
        fresh_bin = sandbox / "fresh.bin"
        fresh_meta = fresh_bin.with_suffix(".json")
        fresh_bin.write_bytes(b"\x00" * (1 * 1024 * 1024))
        fresh_meta.write_text("{}", encoding="utf-8")

        old_bin = sandbox / "old.bin"
        old_meta = old_bin.with_suffix(".json")
        old_bin.write_bytes(b"\x00" * (3 * 1024 * 1024))
        old_meta.write_text("{}", encoding="utf-8")
        old_mtime = time.time() - (_PROG_HEAD_MIN_EVICT_AGE_SEC + 60)
        os.utime(old_bin, (old_mtime, old_mtime))
        os.utime(old_meta, (old_mtime, old_mtime))

        # Redirect real production function to sandbox with tightened cap.
        original_dir = preview_service._PROG_HEAD_DIR
        original_max = preview_service._PROG_HEAD_MAX_BYTES
        preview_service._PROG_HEAD_DIR = sandbox
        preview_service._PROG_HEAD_MAX_BYTES = 1024 * 1024
        try:
            preview_service._prog_head_enforce_budget()
        finally:
            preview_service._PROG_HEAD_DIR = original_dir
            preview_service._PROG_HEAD_MAX_BYTES = original_max

        # Fresh file (mtime=now) is within grace \u2192 not a candidate \u2192 survives.
        # Old file (mtime=cutoff-60) is past grace \u2192 candidate \u2192 over cap \u2192 purged.
        fresh_survived = fresh_bin.exists()
        old_survived = old_bin.exists()

        if not fresh_survived:
            print(f"FAIL: fresh.bin was purged (should be within grace {_PROG_HEAD_MIN_EVICT_AGE_SEC}s)")
            return 1
        if old_survived:
            print("FAIL: old.bin was NOT purged (should be past grace and over cap)")
            return 1

        print(f"OK fresh.bin (age=0s) survived \u2014 within grace ({_PROG_HEAD_MIN_EVICT_AGE_SEC}s)")
        print(f"OK old.bin (age={_PROG_HEAD_MIN_EVICT_AGE_SEC + 60}s) purged \u2014 past grace")
        print("\u2705 prog head eviction grace self-check passed")
        return 0
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1
    finally:
        for p in sandbox.glob("*"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            sandbox.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
