"""The ONE audio-only format spec, shared by every path that wants audio.

WHY THIS IS A MODULE AND NOT A STRING IN EACH CALLER
Three separate call sites wanted audio and each spelled the selector itself.
Two of them used the raw ``bestaudio/best``, which is a defect (below), so a
fix applied to one caller left the other two still burning video bitrate into
a speech recogniser. A single definition cannot drift: the callers now share
one constant, and a test asserts all of them hold that same value.

WHAT ``bestaudio/best`` ACTUALLY DOES — measured, not assumed
yt-dlp reads ``/`` as a fallback CHAIN, and its ``best`` selector means
"best format containing BOTH video and audio". So the moment an extract
returns no audio-only format, the chain silently falls through to a muxed
progressive stream — itag 18 on YouTube, h264+aac — and the caller pulls the
VIDEO bitrate to feed parakeet.

MEASURED on this box, on 10 real YouTube extracts: 0 of 10 offered any
audio-only itag. ffprobe on what was actually fetched: h264 640x360 @
579,939 bps alongside aac @ 48,022 bps. That is ~12x the bytes for a
speech-to-text job, and the failure is SILENT — no error, no warning, just
a slow job that costs bandwidth and looks like it is working.

WHY BOTH RUNGS ARE CONSTRAINED
``bestaudio`` alone is not enough. A real format list on this box also
contains the sb0-sb3 storyboards, which are ``vcodec=none`` too, so a
``vcodec=none``-only tail could hand the transcriber an mhtml storyboard
instead of audio. Hence the tail is ``best[vcodec=none][acodec!=none]``:
still no video, and still genuinely carrying audio.

WHY THAT IS THE SAFER FAILURE
When a format list genuinely has no audio, this spec makes yt-dlp raise
"Requested format is not available". The transcribe worker then REQUEUES.
That is the behaviour we want: the job retries later instead of quietly
downloading five times the data. The unconstrained spelling had the opposite
property — it always succeeded, and always succeeded expensively.
"""
from __future__ import annotations

#: yt-dlp format selector for "audio only, and fail rather than take video".
AUDIO_ONLY_FORMAT_SPEC = "bestaudio/best[vcodec=none][acodec!=none]"

__all__ = ["AUDIO_ONLY_FORMAT_SPEC"]
