"""Self-check: concurrent _silence_stderr must never poison fd 2 (regression
guard for the refcount fix — before it, an interleaved enter/exit restored
devnull as the 'original' fd and stderr died process-wide)."""
import os
import sys
import threading
import time

sys.path.insert(0, ".")
from services.ytdlp_hls import _silence_stderr  # noqa: E402


def worker() -> None:
    for _ in range(80):
        with _silence_stderr():
            os.write(2, b"SHOULD-BE-SILENCED\n")
            time.sleep(0.0005)


threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
os.write(2, b"STDERR_ALIVE\n")
