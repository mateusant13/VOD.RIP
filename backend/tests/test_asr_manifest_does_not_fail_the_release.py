"""An absent ASR runtime must not fail the release.

Measured 2026-10-15: the v1.0.54 tag produced a FAILED release-build job and a
SKIPPED release job, so no GitHub Release was published for it. The cause was
this step alone. `build-asr-runtime` ran and produced nothing at
dist/runtimes/asr/<ver>; the packaging step above correctly warned and skipped
("No ASR runtime at ... - skipping ASR zip (base still built)"); and then
write-asr-runtime-manifest.mjs threw on the very artifact the previous step had
announced it would not create.

The workflow already declares the intent - the base still ships. The script now
reports the absence the same way and exits 0, and it still refuses to write a
manifest describing an archive that is not there: a manifest for a missing
archive is exactly the corrupt state an installer would trust.

RED ARM: put the `throw new Error(...)` back and
test_missing_runtime_is_a_warning_not_a_failure fails, because the script exits 1.
"""
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "write-asr-runtime-manifest.mjs"
MANIFEST = REPO / "release" / "VOD-RIP-ASR-manifest.json"


def _run(version: str):
    return subprocess.run(
        ["node", str(SCRIPT), version],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_missing_runtime_is_a_warning_not_a_failure():
    """RED: restore the throw and this exits 1, taking the release with it."""
    # A version with no runtime anywhere: the shape of the v1.0.54 failure.
    was_there = MANIFEST.exists()
    before = MANIFEST.read_text(encoding="utf-8") if was_there else None
    try:
        if was_there:
            MANIFEST.unlink()
        proc = _run("0.0.0-no-runtime")
        assert proc.returncode == 0, (
            "an absent ASR runtime must not fail the release; got exit %d\n%s\n%s"
            % (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
        )
        # STDOUT, specifically. v1.0.55 exited 0 from this script and the GitHub
        # Actions step still failed; the one difference from the version that
        # worked locally is that the message went to stderr. Pinning the channel
        # is what makes this test able to catch that.
        assert "skipping the manifest" in proc.stdout.lower(), (
            "the warning must go to STDOUT; stderr is what the CI step choked on"
        )
        # and it must NOT invent a manifest for an archive that does not exist
        assert not MANIFEST.exists(), (
            "a manifest describing a missing archive is the corrupt state an"
            " installer would trust; it must not be written"
        )
    finally:
        if was_there and before is not None:
            MANIFEST.write_text(before, encoding="utf-8")


def test_the_script_still_demands_a_version():
    """RED: drop the argv check and a missing version silently writes a 1.0.0 manifest."""
    proc = subprocess.run(
        ["node", str(SCRIPT)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode != 0
    assert "usage" in (proc.stdout + proc.stderr).lower()
