"""Set yt-dlp env before first import — block PO plugins (getpot_wpc spawns headless Chrome)."""

import os

# Hard block — guarded_youtube_dl re-asserts; never use setdefault here.
os.environ["YTDLP_NO_PLUGINS"] = "1"