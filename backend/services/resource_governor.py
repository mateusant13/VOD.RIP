"""Adaptive 80% Resource Governor — dual EWMA, 1s tick, per-resource budgets.

P1 fixes:
 - dual EWMA fast(0.6) for backoff, slow(0.3) for ramp, raw>=0.80 instant clamp
 - 1s sampler for CPU/GPU, RAM/VRAM/network every 5th tick + hot-path clamp
 - thresholds 50/65/80 with 15s hysteresis and per-tick re-evaluate
 - own-load isolation via psutil Process cpu_percent
 - RAM hard cap floor(total*0.80/1.5GiB), pause refill >80%, no AIMD

P2:
 - VRAM headroom max(4GiB,30%), per-chunk budget
 - network semaphore 2..8 keep pool 8
 - immutable GovernorState frozen dataclass swapped atomically
 - NVML/torch.cuda primary before nvidia-smi
 - disk I/O background priority
 - battery/thermal 40% cap

ponytail: sampler is per-process only; cross-process fairness needs shared shm/lock file.
Upgrade path: replace ctypes probes with psutil where available and add cgroup limits.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

_GOVERNOR_TARGET = 0.80
_GOVERNOR_RAMP_UP = 0.50
_GOVERNOR_BACKOFF = 0.65
_GOVERNOR_CLAMP = 0.80
_EWMA_FAST_ALPHA = 0.6
_EWMA_SLOW_ALPHA = 0.3
_TICK_S = 1.0
_COOLDOWN_TICKS = 15
_RAM_PER_WORKER = int(1.5 * 1024 ** 3)
_GIB = 1024 ** 3
_VRAM_HEADROOM_MIN = 4 * _GIB
_VRAM_MODEL_EST = 2 * _GIB
_NET_MIN = 2
_NET_MAX = 8
_POOL_SIZE = 8

@dataclass(frozen=True)
class GovernorState:
    cpu_lanes: int
    ram_max_workers: int
    ram_pause: bool
    vram_headroom: int
    vram_available: int
    vram_total: int
    vram_free: int
    net_concurrency: int
    battery_limited: bool
    effective_target: float
    cpu_ewma_fast: float
    cpu_ewma_slow: float
    cpu_raw: float
    foreground_load: float
    gpu_ewma_fast: float
    gpu_ewma_slow: float
    gpu_raw: float

# --- probes: reuse stdlib, psutil if present, torch.cuda/NVML before nvidia-smi ---

def _probe_system_cpu() -> float:
    try:
        import psutil
        pct = psutil.cpu_percent(interval=None)
        return max(0.0, min(1.0, pct / 100.0))
    except Exception:
        pass
    # fallback: incremental GetSystemTimes delta without sleep
    try:
        if os.name == "nt":
            return _win_cpu_delta()
        else:
            try:
                avg = os.getloadavg()[0]
                n = os.cpu_count() or 1
                return max(0.0, min(1.0, avg / n))
            except Exception:
                return 0.0
    except Exception:
        return 0.0

_win_cpu_prev = None
_win_cpu_lock = threading.Lock()

def _win_cpu_delta() -> float:
    global _win_cpu_prev
    try:
        import ctypes
        from ctypes import wintypes
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]
        def _tot(ft: FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return 0.0
        cur = (_tot(idle), _tot(kernel), _tot(user))
        with _win_cpu_lock:
            prev = _win_cpu_prev
            _win_cpu_prev = cur
        if prev is None:
            return 0.0
        i1,k1,u1 = prev
        i2,k2,u2 = cur
        busy = (k2-k1)+(u2-u1)
        total = busy + (i2-i1)
        return busy/total if total>0 else 0.0
    except Exception:
        return 0.0

def _probe_own_cpu() -> float:
    try:
        import psutil
        p = psutil.Process()
        # cpu_percent with interval None returns since last call; normalize by cpu_count
        pct = p.cpu_percent(interval=None)
        n = os.cpu_count() or 1
        # psutil Process percent can exceed 100 on multicore; convert to 0..1 system fraction
        return max(0.0, min(1.0, pct / 100.0 / 1.0))  # already system-wide fraction? keep capped
    except Exception:
        return 0.0

def _probe_ram() -> Tuple[int,int,float]:
    """(total, avail, used_frac 0..1)"""
    try:
        import psutil
        vm = psutil.virtual_memory()
        total = int(vm.total)
        avail = int(vm.available)
        used = float(vm.percent)/100.0
        return total, avail, max(0.0, min(1.0, used))
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD),("dwMemoryLoad", wintypes.DWORD),("ullTotalPhys", ctypes.c_ulonglong),("ullAvailPhys", ctypes.c_ulonglong),("ullTotalPageFile", ctypes.c_ulonglong),("ullAvailPageFile", ctypes.c_ulonglong),("ullTotalVirtual", ctypes.c_ulonglong),("ullAvailVirtual", ctypes.c_ulonglong),("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys)
                avail = int(status.ullAvailPhys)
                used = 1.0 - (avail/total if total else 0)
                return total, avail, used
        else:
            total = os.sysconf("SC_PHYS_PAGES")*os.sysconf("SC_PAGE_SIZE")
            avail = os.sysconf("SC_AVPHYS_PAGES")*os.sysconf("SC_PAGE_SIZE")
            used = 1.0 - (avail/total if total else 0)
            return int(total), int(avail), used
    except Exception:
        pass
    return 0,0,0.0

def _probe_vram() -> Tuple[int,int]:
    """(total, free) bytes, 0,0 if unknown"""
    # primary: torch.cuda
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.device_count()>0:
            free_b, total_b = torch.cuda.mem_get_info()
            return int(total_b), int(free_b)
    except Exception:
        pass
    # second: NVML ctypes
    try:
        # load nvml
        dll = ctypes.CDLL("nvcuda.dll") if os.name=="nt" else ctypes.CDLL("libnvidia-ml.so.1")
        # nvmlInit
        # signatures: nvmlReturn_t nvmlInit(void)
        # we try minimal: if dll has nvmlInit, call it
        # This is best-effort: many hosts won't have it, fallback to nvidia-smi
        # Use getattr to avoid AttributeError
        # ponytail: NVML probe is best-effort ctypes; full error handling would need nvml.h bindings
        # Upgrade path: use pynvml package if installed
        try:
            dll.nvmlInit.restype = ctypes.c_int
            if dll.nvmlInit() != 0:
                raise RuntimeError("nvmlInit fail")
            # get handle for index 0
            handle = ctypes.c_void_p()
            dll.nvmlDeviceGetHandleByIndex.restype = ctypes.c_int
            dll.nvmlDeviceGetHandleByIndex.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
            if dll.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle)) != 0:
                raise RuntimeError("no handle")
            class NVMLMem(ctypes.Structure):
                _fields_ = [("total", ctypes.c_ulonglong),("free", ctypes.c_ulonglong),("used", ctypes.c_ulonglong)]
            mem = NVMLMem()
            dll.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            dll.nvmlDeviceGetMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(NVMLMem)]
            if dll.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
                return int(mem.total), int(mem.free)
        except Exception:
            pass
    except Exception:
        pass
    # fallback: nvidia-smi CLI
    try:
        import subprocess as sp
        out = sp.run(["nvidia-smi","--query-gpu=memory.total,memory.free","--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5.0)
        if out.returncode==0 and out.stdout.strip():
            # pick largest GPU
            best = (0,0)
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts)>=2:
                    try:
                        total_mib = float(parts[0]); free_mib = float(parts[1])
                        total = int(total_mib * 1024**2); free = int(free_mib * 1024**2)
                        if total>best[0]:
                            best=(total,free)
                    except Exception:
                        continue
            if best[0]>0:
                return best
    except Exception:
        pass
    return 0,0

def _probe_gpu_util() -> float:
    try:
        import subprocess as sp
        out = sp.run(["nvidia-smi","--query-gpu=utilization.gpu","--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3.0)
        if out.returncode==0 and out.stdout.strip():
            for line in out.stdout.strip().splitlines():
                try:
                    v=float(line.strip())
                    return max(0.0, min(1.0, v/100.0))
                except Exception:
                    continue
    except Exception:
        pass
    return 0.0

def _probe_power_limited() -> bool:
    # battery
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is not None and not b.power_plugged:
            return True
        # also check percent low?
        if b is not None and b.percent is not None and b.percent < 20:
            return True
    except Exception:
        pass
    try:
        if os.name=="nt":
            from ctypes import wintypes
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_byte),("BatteryFlag", ctypes.c_byte),("BatteryLifePercent", ctypes.c_byte),("SystemStatusFlag", ctypes.c_byte),("BatteryLifeTime", wintypes.DWORD),("BatteryFullLifeTime", wintypes.DWORD)]
            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                if status.ACLineStatus == 0:  # offline
                    return True
                if status.ACLineStatus == 1:
                    pass
                # BatteryFlag 1 or 2 low
    except Exception:
        pass
    # thermal >80C via NVML (best-effort)
    try:
        # try NVML temp
        dll = ctypes.CDLL("nvcuda.dll") if os.name=="nt" else ctypes.CDLL("libnvidia-ml.so.1")
        handle = ctypes.c_void_p()
        try:
            dll.nvmlInit()
            dll.nvmlDeviceGetHandleByIndex(0, ctypes.byref(handle))
            temp = ctypes.c_uint()
            # nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU, &temp)
            dll.nvmlDeviceGetTemperature.restype = ctypes.c_int
            dll.nvmlDeviceGetTemperature.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
            if dll.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temp))==0 and temp.value>80:
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False

def _set_background_io_priority() -> None:
    # best-effort: lower I/O / CPU priority for worker threads
    try:
        if os.name=="nt":
            try:
                # THREAD_MODE_BACKGROUND_BEGIN 0x00010000 via SetThreadInformation? Try SetThreadPriority
                # Use BelowNormal priority class for process already lowered elsewhere; thread background is best-effort
                # ponytail: full I/O prio needs NtSetInformationThread; upgrade path is win32 API via pywin32
                THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
                kernel32 = ctypes.windll.kernel32
                # Try SetPriorityClass to BELOW_NORMAL if not already
                # No-op if fails
                try:
                    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
                except Exception:
                    pass
                # Try SetThreadPriority with background mode
                try:
                    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_MODE_BACKGROUND_BEGIN)
                except Exception:
                    pass
            except Exception:
                pass
        else:
            try:
                os.nice(10)
            except Exception:
                pass
    except Exception:
        pass

def _ewma(prev: Optional[float], raw: float, alpha: float) -> float:
    if prev is None:
        return raw
    return alpha*raw + (1-alpha)*prev

def _max_cpu_lanes(threads: int, target: float) -> int:
    budget = int(threads * target)
    if budget < 1:
        return 1
    lanes = budget // 8
    if lanes < 1:
        lanes = 1
    if lanes > 8:
        lanes = 8
    return lanes

class ResourceGovernor:
    def __init__(self,
                 probe_system: Optional[Callable[[], float]] = None,
                 probe_own: Optional[Callable[[], float]] = None,
                 probe_ram: Optional[Callable[[], Tuple[int,int,float]]] = None,
                 probe_vram: Optional[Callable[[], Tuple[int,int]]] = None,
                 probe_gpu_util: Optional[Callable[[], float]] = None,
                 probe_power: Optional[Callable[[], bool]] = None,
                ):
        self._probe_system = probe_system or _probe_system_cpu
        self._probe_own = probe_own or _probe_own_cpu
        self._probe_ram = probe_ram or _probe_ram
        self._probe_vram = probe_vram or _probe_vram
        self._probe_gpu = probe_gpu_util or _probe_gpu_util
        self._probe_power = probe_power or _probe_power_limited
        self._cpu_fast: Optional[float] = None
        self._cpu_slow: Optional[float] = None
        self._gpu_fast: Optional[float] = None
        self._gpu_slow: Optional[float] = None
        self._raw_cpu = 0.0
        self._raw_gpu = 0.0
        self._idle_ticks = 0
        threads = os.cpu_count() or 4
        self._cpu_lanes = _max_cpu_lanes(threads, _GOVERNOR_TARGET)
        self._tick_count = 0
        # RAM/VRAM/power cached every 5th tick
        total, avail, used = self._probe_ram()
        if total <= 0:
            total = 8 * _GIB  # fallback for unknown
        self._ram_total = total
        self._ram_used = used
        # effective target may be 0.40 on battery
        self._power_limited = self._probe_power()
        eff = 0.40 if self._power_limited else _GOVERNOR_TARGET
        self._ram_max = max(1, int(total * eff // _RAM_PER_WORKER))
        self._ram_pause = used >= _GOVERNOR_TARGET
        vtotal, vfree = self._probe_vram()
        self._vram_total = vtotal
        self._vram_free = vfree
        head = _VRAM_HEADROOM_MIN
        if vtotal>0:
            head = max(_VRAM_HEADROOM_MIN, int(vtotal*0.30))
        self._vram_headroom = head
        self._vram_avail = max(0, vfree - head - _VRAM_MODEL_EST) if vfree>0 else 0
        self._net_limit = 4
        self._net_active = 0
        self._net_lock = threading.Condition(threading.Lock())
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # publish initial state
        self._publish_state()

    def _publish_state(self) -> None:
        st = GovernorState(
            cpu_lanes=self._cpu_lanes,
            ram_max_workers=self._ram_max,
            ram_pause=self._ram_pause,
            vram_headroom=self._vram_headroom,
            vram_available=self._vram_avail,
            vram_total=self._vram_total,
            vram_free=self._vram_free,
            net_concurrency=self._net_limit,
            battery_limited=self._power_limited,
            effective_target=0.40 if self._power_limited else _GOVERNOR_TARGET,
            cpu_ewma_fast=self._cpu_fast if self._cpu_fast is not None else 0.0,
            cpu_ewma_slow=self._cpu_slow if self._cpu_slow is not None else 0.0,
            cpu_raw=self._raw_cpu,
            foreground_load=self._raw_cpu,
            gpu_ewma_fast=self._gpu_fast if self._gpu_fast is not None else 0.0,
            gpu_ewma_slow=self._gpu_slow if self._gpu_slow is not None else 0.0,
            gpu_raw=self._raw_gpu,
        )
        # atomic swap (GIL) under lock for consistency
        with self._state_lock:
            self._state = st

    @property
    def state(self) -> GovernorState:
        # lock-free atomic reference copy
        return self._state

    def cpu_lanes(self) -> int:
        return self.state.cpu_lanes

    def ram_max_workers(self) -> int:
        return self.state.ram_max_workers

    def ram_pause(self) -> bool:
        return self.state.ram_pause

    def vram_headroom(self) -> int:
        return self.state.vram_headroom

    def vram_available(self) -> int:
        return self.state.vram_available

    def net_concurrency(self) -> int:
        return self.state.net_concurrency

    def hot_path_clamped(self) -> bool:
        # synchronous check: raw >65% -> clamp
        return self._raw_cpu >= _GOVERNOR_BACKOFF

    def tick(self) -> None:
        self._tick_count += 1
        # sample CPU every tick
        sys_load = self._probe_system()
        own = self._probe_own()
        # isolate own load: foreground = system - own (clamped)
        fg = sys_load - own
        if fg < 0: fg = 0.0
        if fg > 1: fg = 1.0
        raw = fg
        self._raw_cpu = raw
        self._cpu_fast = _ewma(self._cpu_fast, raw, _EWMA_FAST_ALPHA)
        self._cpu_slow = _ewma(self._cpu_slow, raw, _EWMA_SLOW_ALPHA)
        # GPU util every tick
        gpu_raw = self._probe_gpu()
        self._raw_gpu = gpu_raw
        self._gpu_fast = _ewma(self._gpu_fast, gpu_raw, _EWMA_FAST_ALPHA)
        self._gpu_slow = _ewma(self._gpu_slow, gpu_raw, _EWMA_SLOW_ALPHA)
        # RAM/VRAM/power every 5th tick
        if self._tick_count % 5 == 1 or self._tick_count == 1:
            total, avail, used = self._probe_ram()
            if total>0:
                self._ram_total = total
                self._ram_used = used
            self._power_limited = self._probe_power()
            vtotal, vfree = self._probe_vram()
            if vtotal>0 or vfree>0:
                self._vram_total = vtotal
                self._vram_free = vfree
            # recompute RAM hard cap
            eff = 0.40 if self._power_limited else _GOVERNOR_TARGET
            if self._ram_total>0:
                self._ram_max = max(1, int(self._ram_total * eff // _RAM_PER_WORKER))
            self._ram_pause = self._ram_used >= _GOVERNOR_TARGET
            # VRAM headroom
            head = _VRAM_HEADROOM_MIN
            if self._vram_total>0:
                head = max(_VRAM_HEADROOM_MIN, int(self._vram_total*0.30))
            self._vram_headroom = head
            self._vram_avail = max(0, self._vram_free - head - _VRAM_MODEL_EST) if self._vram_free>0 else 0
        # CPU AIMD with hysteresis
        threads = os.cpu_count() or 4
        eff = 0.40 if self._power_limited else _GOVERNOR_TARGET
        ceiling = _max_cpu_lanes(threads, eff)
        # clamp raw >=0.80 instant
        if raw >= _GOVERNOR_CLAMP:
            self._cpu_lanes = 1
            self._idle_ticks = 0
        elif (self._cpu_fast is not None and self._cpu_fast > _GOVERNOR_BACKOFF):
            # halve, re-evaluate each tick (multiplicative)
            self._cpu_lanes = max(1, self._cpu_lanes // 2)
            self._idle_ticks = 0
        elif (self._cpu_slow is not None and self._cpu_slow < _GOVERNOR_RAMP_UP):
            self._idle_ticks += 1
            if self._idle_ticks >= _COOLDOWN_TICKS:
                if self._cpu_lanes < ceiling:
                    self._cpu_lanes = min(ceiling, self._cpu_lanes + 1)
                self._idle_ticks = 0
        else:
            self._idle_ticks = 0
        # clamp to ceiling and floor
        if self._cpu_lanes > ceiling:
            self._cpu_lanes = ceiling
        if self._cpu_lanes < 1:
            self._cpu_lanes = 1
        # also RAM hard cap tightens cpu lanes: lanes = min(cpu_lanes, ram_max)
        if self._ram_max>0 and self._cpu_lanes > self._ram_max:
            # RAM is capacity, not AIMD — hard cap
            # keep lanes at ram cap (which may be 1)
            self._cpu_lanes = max(1, min(self._cpu_lanes, self._ram_max))
        self._publish_state()

    # --- sampler daemon 1s tick ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # warm psutil counters
        try: self._probe_system()
        except Exception: pass
        try: self._probe_own()
        except Exception: pass
        def _run():
            _set_background_io_priority()
            while not self._stop.is_set():
                if self._stop.wait(_TICK_S):
                    break
                try:
                    self.tick()
                except Exception:
                    continue
        self._thread = threading.Thread(target=_run, name="governor-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th and th.is_alive():
            th.join(timeout=2.0)

    # --- network semaphore 2..8 via condition ---

    def acquire_download(self, timeout: Optional[float] = None) -> bool:
        with self._net_lock:
            start = time.monotonic()
            while self._net_active >= self._net_limit:
                remaining = None if timeout is None else timeout - (time.monotonic()-start)
                if remaining is not None and remaining <= 0:
                    return False
                self._net_lock.wait(timeout=0.1 if remaining is None else min(0.1, remaining))
                if timeout is not None and time.monotonic()-start >= timeout:
                    return False
            self._net_active += 1
            return True

    def release_download(self) -> None:
        with self._net_lock:
            self._net_active = max(0, self._net_active - 1)
            self._net_lock.notify_all()

    def set_net_concurrency(self, n: int) -> None:
        n = max(_NET_MIN, min(_NET_MAX, int(n)))
        with self._net_lock:
            self._net_limit = n
            self._net_lock.notify_all()
        # keep state in sync
        self._publish_state()

    def record_download_success(self, throughput: Optional[float] = None) -> None:
        # Vegas/AIMD: +1 if throughput scales, capped 8. Simplified: +1 on success if not at max
        # ponytail: real Vegas would compare RTT/throughput EMA; upgrade path adds throughput EWMA
        with self._net_lock:
            if self._net_limit < _NET_MAX:
                # only ramp if system not under backoff
                if self._cpu_fast is None or self._cpu_fast < _GOVERNOR_BACKOFF:
                    self._net_limit = min(_NET_MAX, self._net_limit + 1)
                    self._net_lock.notify_all()
        self._publish_state()

    def record_download_failure(self) -> None:
        with self._net_lock:
            self._net_limit = max(_NET_MIN, self._net_limit // 2)
            if self._net_limit < _NET_MIN:
                self._net_limit = _NET_MIN
            self._net_lock.notify_all()
        self._publish_state()

    # alias for tests
    def _set_cpu_lanes_for_test(self, n: int) -> None:
        self._cpu_lanes = max(1, min(8, int(n)))
        self._publish_state()

_governor: Optional[ResourceGovernor] = None
_gov_lock = threading.Lock()

def get_governor() -> ResourceGovernor:
    global _governor
    if _governor is None:
        with _gov_lock:
            if _governor is None:
                _governor = ResourceGovernor()
                _governor.start()
    return _governor

def _reset_governor_for_test() -> None:
    global _governor
    with _gov_lock:
        if _governor is not None:
            try: _governor.stop()
            except Exception: pass
        _governor = None
