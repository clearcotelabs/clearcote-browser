"""docker/idle_exit.py, the Docker image's idle exit (CC_IDLE_EXIT_SECONDS): the countdown starts when
Chrome's DevTools endpoint first answers, never at Chrome's start, so a slow start (a cold machine, a
first-run engine download) does not eat the time a client has to connect. Loaded from the repository's
docker/ directory; skipped when the tree does not have it."""
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.normpath(os.path.join(HERE, "..", "..", "..", "docker", "idle_exit.py"))
pytestmark = pytest.mark.skipif(not os.path.exists(PATH), reason="no docker/idle_exit.py in this tree")


def _module():
    spec = importlib.util.spec_from_file_location("idle_exit", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(ready_at, clients_until=None, limit=5, alive_for=1000):
    """watch() on a fake clock: CDP answers from ``ready_at`` s, a client is connected until
    ``clients_until`` s. Returns (stopped?, when)."""
    mod = _module()
    clock = Clock()
    stopped = []
    mod.watch(limit, alive=lambda: not stopped and clock.t < alive_for, ready=lambda: clock.t >= ready_at,
              clients=lambda: 1 if clients_until is not None and clock.t < clients_until else 0,
              stop=lambda: stopped.append(clock.t), clock=clock, sleep=clock.sleep, log=lambda m: None)
    return bool(stopped), (stopped[0] if stopped else None)


def test_the_countdown_starts_when_cdp_answers_not_at_chrome_start():
    # Chrome takes 20 s to answer and nobody connects: stopped `limit` seconds after it answered, not
    # 5 s after it was started (which would kill it before any client could reach it).
    stopped, when = run(ready_at=20, limit=5)
    assert stopped and 25 <= when <= 26.5


def test_a_connected_client_keeps_it_and_the_countdown_restarts_when_it_goes():
    stopped, when = run(ready_at=2, clients_until=60, limit=5)
    assert stopped and 64 <= when <= 66.5  # 5 s after the client was last seen (sampled every second)


def test_a_browser_that_ends_on_its_own_is_not_stopped():
    assert run(ready_at=1000, limit=5, alive_for=30) == (False, None)


def test_cdp_clients_counts_established_connections_to_the_port(tmp_path):
    mod = _module()
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 00000000:2406 00000000:0000 0A\n"   # the listening socket: not a client
        "   1: 020011AC:2406 010011AC:D2F0 01\n"   # a client on :9222
        "   2: 0100007F:240F 0100007F:9A3C 01\n"   # chrome's own :9231 side: another port
        "   3: 020011AC:2406 010011AC:D2F2 06\n")  # TIME_WAIT: gone
    assert mod.cdp_clients(9222, tables=(str(table),)) == 1
