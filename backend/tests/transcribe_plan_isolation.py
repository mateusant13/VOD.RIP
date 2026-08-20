"""Isolate _worker_plan tests from process-global governor + CPU load cache."""

from __future__ import annotations

from services import archive_transcribe as at

GIB = 1024 ** 3


def reset_cpu_load_cache() -> None:
    at._cpu_load_high_cache = False
    at._cpu_load_at = 0.0


def stub_idle_governor(monkeypatch) -> None:
    if not getattr(at, "_GOVERNOR_AVAILABLE", False):
        return
    from services.resource_governor import GovernorState

    idle = GovernorState(
        cpu_lanes=8,
        ram_max_workers=64,
        ram_pause=False,
        vram_headroom=4 * GIB,
        vram_available=64 * GIB,
        vram_total=64 * GIB,
        vram_free=64 * GIB,
        net_concurrency=8,
        battery_limited=False,
        effective_target=0.8,
        cpu_ewma_fast=0.0,
        cpu_ewma_slow=0.0,
        cpu_raw=0.0,
        foreground_load=0.0,
        gpu_ewma_fast=0.0,
        gpu_ewma_slow=0.0,
        gpu_raw=0.0,
    )

    class _IdleGov:
        state = idle

        def cpu_lanes(self) -> int:
            return 8

        def ram_max_workers(self) -> int:
            return 64

        def ram_pause(self) -> bool:
            return False

    monkeypatch.setattr(at, "get_governor", lambda: _IdleGov())


def isolate_worker_plan(monkeypatch) -> None:
    reset_cpu_load_cache()
    stub_idle_governor(monkeypatch)
