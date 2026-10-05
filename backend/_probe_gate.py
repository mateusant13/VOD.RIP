"""Probe: replicate the overtake test's interleaving and log every event."""
import sys, threading, time
sys.path.insert(0, ".")

from services import ytdlp_guard

gate = ytdlp_guard.priority_lock
gate.reset()
events = []
t0 = time.monotonic()


def log(what):
    events.append((round(time.monotonic() - t0, 3), what,
                   [(w[0], w[1]) for w in gate._waiters], gate._busy))


bg_running = threading.Event()
bg_done = threading.Event()


def bg():
    with gate.acquire(kind="background"):
        log("bg IN")
        bg_running.set()
        bg_done.wait(10)
    log("bg OUT")


walk2_queued = threading.Event()
walk2_done = threading.Event()


def walk2():
    walk2_queued.set()
    with gate.acquire(kind="background"):
        log("walk2 IN")
    walk2_done.set()
    log("walk2 OUT")


preview_done = threading.Event()


def preview():
    with gate.acquire(kind="interactive"):
        log("preview IN")
    preview_done.set()


t_bg = threading.Thread(target=bg, daemon=True)
t_bg.start()
assert bg_running.wait(5)

t_w2 = threading.Thread(target=walk2, daemon=True)
t_w2.start()
assert walk2_queued.wait(5)
time.sleep(0.15)

t_pv = threading.Thread(target=preview, daemon=True)
t_pv.start()
time.sleep(0.15)

log("-- releasing bg --")
bg_done.set()
t_bg.join(5)
preview_done.wait(5)
walk2_done.wait(5)
t_w2.join(5)
t_pv.join(5)

for t, what, waiters, busy in events:
    print("%6.3fs  %-14s waiters=%s busy=%s" % (t, what, waiters, busy))
