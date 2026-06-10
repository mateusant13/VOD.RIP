#!/usr/bin/env python3
"""Thin wrapper — runs `debug_cli full --spawn-server`.

Prefer:  python KickDownloader/KickDownloaderPy/debug_cli.py full --spawn-server
     or:  python KickDownloader/KickDownloaderPy/run.py --debug full --spawn-server
"""
import subprocess
import sys
from pathlib import Path

_CLI = Path(__file__).resolve().parent / "KickDownloader" / "KickDownloaderPy" / "debug_cli.py"
argv = [sys.executable, str(_CLI), "full", "--spawn-server", *sys.argv[1:]]
raise SystemExit(subprocess.call(argv))
