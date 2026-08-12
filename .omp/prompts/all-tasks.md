# VOD.RIP ALL TASKS
Use omp task tool + git worktrees. Skip formatters/linters/full tests. No Cursor Task. No MCP.

CONSTRAINTS: Parakeet DEFAULT ASR. Whisper large-v3-turbo ONLY when Parakeet itself failed or language ja/ko/zh/ar. Not Parakeet-only. PT UI.

TASK1 Thumbnails: clip history + any download without thumb. Extract early on Clip click and download. Persist path. ChannelClipThumb DownloadThumb TwitchClipPopup download_manager ytdlp_download archive_db twitch_clips App types

TASK2 Clear notifications: Limpar notificacoes persisted. NotificationsPanel QueueTab jobs API

TASK3 Mini preview: REMOVE URL next to Clip. Same trim as main preview. Download button plays exact time on MAIN preview, hide mini keep in memory. Main gets Voltar ao mini only then. ChannelExplorePopup App layoutUtils preview

TASK4 Clip filters Hoje/1-7d auto-advance to next filter with clips. ChannelExplorePopup channelUtils

TASK5 Remove VIDEO search chip keep only FALA. ArchiveSearchPopup archiveSearchUtils archive.py archive_db i18n

TASK6 Mode EXATO/AMPLA/CONTEXTO auto re-search. EXATO contiguous phrase: vale da estranheza MUST NOT match ela ta gastando a estranheza do guaxi vey. archive_db archive.py ArchiveSearchPopup

TASK7 No 300 result cap. Return all. Optimize. Virtualize UI.

TASK8 Chatterino-like 24/7 TMI: log only exact channel-name mentions from user channel list. Searchable fallback. Autostart even if UI closed.

TASK9 Parakeet default settings. Whisper turbo only on Parakeet failure. Improve autodetect. archive_transcribe TranscriptionSection schemas App

TASK10 Immortal disk retry queue for failed background jobs. Honor rate limits. Fewer failed notifications.

Write .omp/prompts/DONE.md with 1-10 DONE/BLOCKED and files.
