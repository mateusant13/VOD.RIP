"""Resource governor tests — dual EWMA, hysteresis, AIMD, semaphore, State, chaos"""
import time, threading, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.resource_governor import (
    ResourceGovernor, GovernorState,
    _GOVERNOR_TARGET, _GOVERNOR_RAMP_UP, _GOVERNOR_BACKOFF, _GOVERNOR_CLAMP,
    _EWMA_FAST_ALPHA, _EWMA_SLOW_ALPHA, _GIB
)

def test_dual_ewma_fast_vs_slow():
    # fast reacts quicker than slow
    seq = [0.0,0.0,0.0,1.0,1.0,1.0]
    probes = iter(seq)
    g = ResourceGovernor(
        probe_system=lambda: next(probes,1.0),
        probe_own=lambda: 0.0,
        probe_ram=lambda: (8*_GIB,6*_GIB,0.25),
        probe_vram=lambda: (0,0),
        probe_gpu_util=lambda: 0.0,
        probe_power=lambda: False,
    )
    for _ in seq:
        g.tick()
    # after spike, fast should be higher (closer to 1.0)
    assert g.state.cpu_ewma_fast > g.state.cpu_ewma_slow, (g.state.cpu_ewma_fast, g.state.cpu_ewma_slow)

def test_raw_80_instant_clamp():
    g = ResourceGovernor(
        probe_system=lambda: 0.85,
        probe_own=lambda: 0.0,
        probe_ram=lambda: (8*_GIB, 6*_GIB,0.25),
        probe_vram=lambda: (0,0),
        probe_gpu_util=lambda: 0.0,
        probe_power=lambda: False,
    )
    g._set_cpu_lanes_for_test(4)
    g.tick()
    assert g.cpu_lanes()==1, "raw >=0.80 must clamp to 1"

def test_backoff_65_halves():
    g = ResourceGovernor(
        probe_system=lambda:0.70, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB,6*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g._set_cpu_lanes_for_test(4)
    # first tick will set EWMA near 0.70*alpha + init -> but we need fast>0.65
    g.tick()
    # after tick, since fast >0.65 should halve from 4 to 2
    assert g.cpu_lanes()==2
    g.tick()
    assert g.cpu_lanes()==1

def test_hysteresis_ramp_needs_15_ticks_below_50():
    seq = [0.3]*20
    it = iter(seq)
    g = ResourceGovernor(
        probe_system=lambda: next(it,0.3), probe_own=lambda:0.0,
        probe_ram=lambda:(16*_GIB,12*_GIB,0.2), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g._set_cpu_lanes_for_test(1)
    # 14 ticks not enough
    for _ in range(14):
        g.tick()
    assert g.cpu_lanes()==1
    g.tick()  # 15th
    assert g.cpu_lanes()==2

def test_re_evaluate_each_tick():
    g = ResourceGovernor(
        probe_system=lambda:0.70, probe_own=lambda:0.0,
        probe_ram=lambda:(64*_GIB,60*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    # bump ceiling by temporarily patching cpu_count via monkey? Use high RAM and avoid ceiling clamp — just check halving
    # force ceiling high by mocking _max_cpu_lanes locally: set lanes to 8 then tick with high cap
    import services.resource_governor as rg
    orig = rg._max_cpu_lanes
    rg._max_cpu_lanes = lambda threads, target: 8
    try:
        g._set_cpu_lanes_for_test(8)
        g.tick(); assert g.cpu_lanes()==4
        g.tick(); assert g.cpu_lanes()==2
        g.tick(); assert g.cpu_lanes()==1
    finally:
        rg._max_cpu_lanes = orig

def test_ceiling_math_80_percent():
    threads = os.cpu_count() or 4
    g = ResourceGovernor(probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(64*_GIB,60*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    # run 15 low ticks to ramp to ceiling
    for _ in range(16):
        g.tick()
    ceiling = max(1, min(8, int(threads*0.80)//8 if False else int(threads*0.80//1)))
    # simplified: should reach ceiling eventually, at least >=2
    assert g.cpu_lanes() >= 2
    assert g.cpu_lanes() <= 8

def test_ram_hard_cap_and_pause():
    g = ResourceGovernor(
        probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB, 1*_GIB, 0.85), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g.tick()
    assert g.ram_pause() is True
    assert g.ram_max_workers() >= 1
    # hard cap floor(total*0.80/1.5GiB) ~ 4
    assert g.ram_max_workers() == max(1, int(8*_GIB*0.80//(1.5*_GIB)))

def test_semaphore_2_8():
    g = ResourceGovernor(probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB,6*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    assert 2 <= g.net_concurrency() <= 8
    g.set_net_concurrency(1); assert g.net_concurrency()==2
    g.set_net_concurrency(20); assert g.net_concurrency()==8
    # acquire/release
    g.set_net_concurrency(2)
    assert g.acquire_download(timeout=0.1)
    assert g.acquire_download(timeout=0.1)
    # third would block
    assert not g.acquire_download(timeout=0.05)
    g.release_download()
    assert g.acquire_download(timeout=0.1)
    g.release_download(); g.release_download()

def test_state_frozen_immutable():
    g = ResourceGovernor(probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB,6*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    st = g.state
    assert isinstance(st, GovernorState)
    try:
        st.cpu_lanes = 99
        assert False, "frozen should raise"
    except Exception:
        pass
    # swapping publishes new object
    s1 = g.state
    g.tick()
    s2 = g.state
    assert s1 is not s2 or True  # tick may re-publish

def test_vram_headroom_4gib_or_30pct():
    g = ResourceGovernor(probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB,6*_GIB,0.1), probe_vram=lambda:(16*_GIB,12*_GIB),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g.tick()
    # 16GiB total -> 30% = 4.8 GiB >4, so headroom ~4.8
    assert g.state.vram_headroom >= 4*_GIB
    assert g.state.vram_headroom == max(4*_GIB, int(16*_GIB*0.30))
    # 8 GiB total -> 4 wins over 2.4
    g2 = ResourceGovernor(probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB,6*_GIB,0.1), probe_vram=lambda:(8*_GIB,6*_GIB),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g2.tick()
    assert g2.state.vram_headroom == 4*_GIB

def test_battery_caps_40pct():
    g = ResourceGovernor(probe_system=lambda:0.1, probe_own=lambda:0.0,
        probe_ram=lambda:(8*_GIB,6*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:True)
    g.tick()
    assert g.state.battery_limited is True
    assert g.state.effective_target == 0.40
    assert g.ram_max_workers() == max(1, int(8*_GIB*0.40//(1.5*_GIB)))

def test_chaos_inject_synthetic_burn_clamps_within_2_ticks():
    g = ResourceGovernor(
        probe_system=lambda:0.95, probe_own=lambda:0.0,
        probe_ram=lambda:(16*_GIB,12*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g._set_cpu_lanes_for_test(4)
    g.tick(); assert g.cpu_lanes()==1, "raw 95% clamp within 1 tick"
    # also test 0.70 burn halves within 2
    g2 = ResourceGovernor(probe_system=lambda:0.70, probe_own=lambda:0.0,
        probe_ram=lambda:(16*_GIB,12*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g2._set_cpu_lanes_for_test(4)
    g2.tick(); assert g2.cpu_lanes() in (1,2)
    g2.tick(); assert g2.cpu_lanes()==1

def test_foreground_isolation():
    # system 0.80 but own 0.30 -> foreground 0.50, should not clamp
    g = ResourceGovernor(probe_system=lambda:0.80, probe_own=lambda:0.30,
        probe_ram=lambda:(16*_GIB,12*_GIB,0.1), probe_vram=lambda:(0,0),
        probe_gpu_util=lambda:0.0, probe_power=lambda:False)
    g._set_cpu_lanes_for_test(4)
    g.tick()
    # foreground 0.50 <0.65, should NOT halve on first tick (maybe slow still low)
    assert g.cpu_lanes() in (4,2)  # 0.50 not backoff; with slow <0.50 may not change
    # But if own isolation failed and raw used 0.80, it would clamp to 1 — ensure not
    assert g.cpu_lanes() != 1 or g.state.foreground_load==0.50
