"""A release step without a deadline turns a flaky CDN into an eternal job.

Measured 2026-10-15, run 38779549411: release-build sat on
"Download Microsoft WebView2 bootstrapper" for 35+ minutes. Every later step,
including the manifest step just fixed, was still "pending", so the run never
concluded. Invoke-WebRequest has no default timeout in PowerShell 7 and
go.microsoft.com is a redirector that does not always answer.

That is also why the earlier failures looked inconsistent: the same download
SUCCEEDED in v1.0.54/55/57/58 (those runs reached the manifest step and failed
there) and did not answer for v1.0.59. Two different symptoms, one workflow, and
the second one hides behind a job that simply never finishes.

RED ARM: delete -TimeoutSec 90 and
test_the_webview2_download_has_a_deadline fails.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"


def _code(body: str) -> str:
    """Strip YAML comments before asserting.

    Learned the hard way, twice: the first version of the deadline test passed
    with the fix REMOVED, because the step own comment says "-TimeoutSec is per
    attempt. The default is infinite." and the assertion was matching the
    comment rather than the code. A guard that reads documentation is a guard
    that cannot fail. Same trap as the Promise.all check.
    """
    return "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))


def _step_body(name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = f"- name: {name}"
    assert marker in text, f"step not found: {name}"
    rest = text.split(marker, 1)[1]
    # stop at the next step definition
    return rest.split("\n      - name:", 1)[0]


def test_the_webview2_download_has_a_deadline():
    """RED: remove -TimeoutSec and this fails, and the release hangs again."""
    body = _code(_step_body("Download Microsoft WebView2 bootstrapper"))
    assert "Invoke-WebRequest" in body, body[:200]
    assert "-TimeoutSec" in body, (
        "Invoke-WebRequest has no default timeout; without -TimeoutSec a flaky"
        " fwlink hangs the release-build job indefinitely (measured: 35+ min)"
    )
    assert "foreach ($attempt in 1..3)" in body, "the download must retry"


def test_a_failed_download_fails_loudly_instead_of_proceeding():
    """RED: drop the throw and a truncated bootstrapper reaches ISCC."""
    body = _code(_step_body("Download Microsoft WebView2 bootstrapper"))
    assert "throw" in body, (
        "a missing WebView2 bootstrapper must fail the step; the installer"
        " bundles it and ISCC would fail later with a less clear message"
    )
    assert "Test-Path $out" in body, "the file must be verified after download"
