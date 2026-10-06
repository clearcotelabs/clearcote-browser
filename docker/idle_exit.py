"""Idle exit for the clearcote container (serve.py's CC_IDLE_EXIT_SECONDS). Standard library only.

A connected CDP client keeps one WebSocket open for as long as it is connected, so "a client is connected"
is an ESTABLISHED TCP connection to the published port, read from /proc/net/tcp{,6} (the container's own
network namespace). The countdown starts when Chrome's DevTools endpoint first answers, never before:
how long Chrome takes to come up (a cold start, a slow machine, a first-run engine download) must not
eat into the time a client has to connect.
"""
import json
import time
import urllib.request


def cdp_clients(listen_port, tables=("/proc/net/tcp", "/proc/net/tcp6")):
    """ESTABLISHED connections whose local port is ``listen_port``."""
    suffix = ":%04X" % int(listen_port)
    n = 0
    for table in tables:
        try:
            with open(table) as fh:
                next(fh, None)
                for line in fh:
                    fields = line.split()
                    if len(fields) > 3 and fields[1].endswith(suffix) and fields[3] == "01":  # 01 = ESTABLISHED
                        n += 1
        except OSError:
            pass
    return n


def cdp_ready(port, timeout=1.0):
    """True once Chrome's DevTools endpoint on 127.0.0.1:``port`` answers /json/version."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/json/version" % port, timeout=timeout) as r:  # noqa: S310
            return r.status == 200 and "webSocketDebuggerUrl" in json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 -- not up yet
        return False


def watch(limit, alive, ready, clients, stop, clock=time.monotonic, sleep=time.sleep, log=print):
    """Call ``stop()`` once no client has been connected for ``limit`` seconds, counted from the moment
    ``ready()`` first holds (Chrome's DevTools answering). Returns True when it stopped the browser, False
    when the browser ended on its own first."""
    while alive() and not ready():
        sleep(0.25)
    if not alive():
        return False
    last = clock()
    while alive():
        sleep(1)
        if clients() > 0:
            last = clock()
        elif clock() - last >= limit:
            log("[clearcote] no CDP client for %d s (CC_IDLE_EXIT_SECONDS): stopping" % limit)
            stop()
            return True
    return False
