"""A GPU ASR runtime archive is bigger than Node can read in one buffer.

Measured in CI 2026-10-15 (gh run view 38775244925 --log-failed):

  RangeError [ERR_FS_FILE_TOO_LARGE]: File size (2150340321) is greater than 2 GiB
      at tryCreateBuffer (node:fs:410:13)
      at readFileSync (node:fs:463:14)
      at sha256 (scripts/write-asr-runtime-manifest.mjs:48:38)

readFileSync() cannot read a file above 2 GiB on Node 20. The archive is
2.15 GB, so sha256() could never hash it, and EVERY tag from v1.0.54 on failed
release-build here and skipped the release job entirely - four tags shipped no
release while the runtime itself was built correctly every time.

Three wrong theories preceded this one, and they are why the test is written
against the real script instead of a unit-tested helper: a missing runtime
(wrong - it is 2.15 GB), a stderr channel problem (wrong - it exits 0 either
way), and a missing guard (wrong - the artifact exists, so the guard never
applied). None of them could be checked locally because a 2 GiB file cannot be
made quickly by writing to it. A SPARSE file can, and that is what this test
does: it builds the real directory layout the script expects, with a >2 GiB
sparse archive, and drives the script end to end.

RED ARM: restore readFileSync in sha256() and this test fails with
ERR_FS_FILE_TOO_LARGE, which is the CI failure reproduced locally.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "write-asr-runtime-manifest.mjs"
MANIFEST = REPO / "release" / "VOD-RIP-ASR-manifest.json"
VERSION = "0.0.0-bigarchive"
RUNTIME_DIR = REPO / "dist" / "runtimes" / "asr" / VERSION
ARCHIVE = REPO / "release" / f"VOD-RIP-ASR-{VERSION}-win-x64.zip"
TWO_GIB = 2 * 1024 ** 3


def _sparse(path: Path, size: int) -> None:
    """A file of `size` bytes that occupies almost nothing on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)


@pytest.fixture()
def big_archive():
    """The real layout, with a 2 GiB + 64 KiB sparse zip. Cleaned up either way."""
    had_manifest = MANIFEST.exists()
    keep = MANIFEST.read_bytes() if had_manifest else None
    MANIFEST.unlink(missing_ok=True)
    _sparse(ARCHIVE, TWO_GIB + 65536)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    (RUNTIME_DIR / "VOD-RIP-ASR.exe").write_bytes(b"stub")
    (RUNTIME_DIR / "cudnn64_9.dll").write_bytes(b"stub-dll")
    try:
        yield
    finally:
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        ARCHIVE.unlink(missing_ok=True)
        MANIFEST.unlink(missing_ok=True)
        if keep is not None:
            MANIFEST.write_bytes(keep)


def test_hashes_an_archive_larger_than_two_gib(big_archive):
    """RED: put readFileSync back in sha256() and this fails ERR_FS_FILE_TOO_LARGE."""
    proc = subprocess.run(
        ["node", str(SCRIPT), VERSION],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert proc.returncode == 0, (
        "a 2 GiB archive must not fail the manifest step; exit %d\n%s\n%s"
        % (proc.returncode, proc.stdout[-600:], proc.stderr[-900:])
    )
    assert "ERR_FS_FILE_TOO_LARGE" not in (proc.stdout + proc.stderr)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # every file under the runtime dir is recorded, hashed with a stream
    assert set(manifest["files"]) == {"VOD-RIP-ASR.exe", "cudnn64_9.dll"}, manifest["files"]
    assert len(manifest["archive_sha256"]) == 64
    assert manifest["version"] == VERSION

def test_hashes_a_runtime_with_thousands_of_files():
    """A torch tree has thousands of files; hashing them must not open them all.

    Measured in CI 2026-10-15 (run 38777288432):

      [Error: EMFILE: too many open files, open
       '...\\dist\\runtimes\\asr\\1.0.58\\_internal\\torchaudio\\models\\wav2vec2\\
       components.py']

    The stream fix for the 2 GiB archive collected the per-file hashes with
    Promise.all, which opens every file at once. The GPU runtime is a torch
    tree, so the descriptor table ran out. A sequential loop costs seconds and
    cannot exhaust anything.

    RED ARM ATTEMPTED AND FAILED, and that is recorded rather than hidden: with
    CI failure reproduced locally.
    """
    had_manifest = MANIFEST.exists()
    keep = MANIFEST.read_bytes() if had_manifest else None
    MANIFEST.unlink(missing_ok=True)
    files = 4000
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        # A REAL archive, not sparse: this test is about descriptors, and the
        # file count is the thing that matters. Each file is a few bytes.
        for i in range(files):
            (RUNTIME_DIR / f"mod_{i:05d}.py").write_bytes(b"x = 1\n")
        _sparse(ARCHIVE, 4096)  # small: the 2 GiB case is the other test
        proc = subprocess.run(
            ["node", str(SCRIPT), VERSION],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        assert proc.returncode == 0, (
            "%d files must not exhaust descriptors; exit %d\n%s\n%s"
            % (files, proc.returncode, proc.stdout[-500:], proc.stderr[-900:])
        )
        assert "EMFILE" not in (proc.stdout + proc.stderr)
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert len(manifest["files"]) == files, len(manifest["files"])
    finally:
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        ARCHIVE.unlink(missing_ok=True)
        MANIFEST.unlink(missing_ok=True)
        if keep is not None:
            MANIFEST.write_bytes(keep)

def test_the_hash_pass_is_sequential_not_unbounded():
    """The falsifiable version of the EMFILE fix, since size does not decide it.

    CI died with EMFILE opening thousands of runtime files at once, because the
    per-file hashes were collected with Promise.all. The descriptor ceiling
    depends on the runner, so a local file-count test could not reproduce it -
    measured: 4000 files with Promise.all restored still passed here. What CAN
    be pinned locally is the property that actually prevents it: hashing must
    be sequential. If someone reintroduces unbounded concurrency, this fails.

    RED ARM: restore the Promise.all and this fails immediately.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("//")
    )
    assert "Promise.all" not in code, (
        "the hash pass must not open every runtime file at once: the GPU runtime"
        " is a torch tree of thousands of files and EMFILE killed release-build"
    )
    assert "for (const [name, path] of entries)" in code, (
        "the hash pass must iterate files one at a time"
    )