# Plan: 10/10 Across All Categories

## Current Scores: 4, 3, 4, 6, 3, 3, 7, 4 → Average 4.25/10

## Architecture (4→10)
- [ ] Split `backend/main.py` (1,686 lines) into: `routers/downloads.py`, `routers/preview.py`, `routers/channels.py`, `routers/settings.py`, `middleware.py`, `app.py`
- [ ] Finish App.tsx decomposition: extract remaining 75 module-level functions into focused modules
- [ ] Split `backend/services/ytdlp_service.py` (2,337 lines) into: `ytdlp_download.py`, `ytdlp_ffmpeg.py`, `ytdlp_cache.py`

## Maintainability (3→10)
- [ ] Narrow all ~80 `except Exception:` handlers to specific exception types
- [ ] Eliminate duplicate utility functions: extract `parseHms`, `formatHmsFull`, `clamp` to shared modules
- [ ] Remove global mutable state in `preview_service.py`, `app_lifecycle.py`, `server_lifecycle.py`
- [ ] Remove dead code: deprecated `SavedChannel.errors`, stale fallback in `flushPanelLayoutToBackend`

## Operational Reliability (4→10)
- [ ] Fix shutdown race in `app_lifecycle.py` — use non-daemon threads + explicit join with timeout
- [ ] Add atomic write recovery for temp file orphans
- [ ] Fix `_enforce_deadline()` to not run on every progress tick
- [ ] Add proper curl_cffi shutdown to prevent interpreter-exit crashes

## Release Readiness (6→10)
- [ ] Make Authenticode signing mandatory (fail build if not signed)
- [ ] Add update rollback capability (versioned backup before update)
- [ ] Add SRI integrity check for inlined frontend assets
- [ ] Test Linux build end-to-end

## Simplicity (3→10)
- [ ] Remove `os_services.py` platform-detection abstraction (only Windows used)
- [ ] Consolidate `_NO_WINDOW` usage — standardize on one pattern
- [ ] Remove deprecated migration code (`normalizeSavedChannel` legacy paths)

## Contributor Experience (3→10)
- [ ] Add component tests for all 10 frontend components
- [ ] Add API route tests (FastAPI TestClient)
- [ ] Add integration test for end-to-end download flow
- [ ] Reduce largest files: App.tsx < 2,500, main.py < 800, ytdlp_service.py < 1,000

## Dependency Hygiene (7→10)
- [ ] Audit for unused or duplicate dependencies
- [ ] Pin exact versions in requirements.txt
- [ ] Add Dependabot config

## Production Confidence (4→10)
- [ ] Achieve >60% test coverage
- [ ] Add structured logging (not just `logger.exception`)
- [ ] Add health check endpoint
- [ ] Add crash recovery for settings/history corruption
