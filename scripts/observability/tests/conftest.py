"""Shared fixtures for the observability substrate's own tests.

Point the gate's log directory at a private temp dir for the whole session. The
tests drive ExecCheck directly, and without this they write fixture logs
(``ghost.log``, ``noisy.log``, ``hang.log``) into the real gate's
``tmp/verify-logs`` next to ``vitest.log`` - where a reader would reasonably
conclude the gate had run a command named "ghost".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import verify_gate as gate  # noqa: E402


@pytest.fixture(autouse=True)
def _private_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "LOG_DIR", tmp_path / "gate-logs")
    yield
