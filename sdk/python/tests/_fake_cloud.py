"""An in-memory stand-in for the hosted Clearcote API (PLAN §3), for the cloud client and CLI tests.

Implements exactly the endpoints the SDK calls, with the documented shapes, so a test can assert both
what the SDK sent and what it made of the answer. Deterministic on purpose: the cross-language CLI
parity test runs the Python and the Node CLI against two fresh instances and compares stdout.

    api = start_fake_cloud()                 # api.url, api.log, api.close()
    api.run_statuses = ["queued", "succeeded"]  # what successive GET /runs/{id} answer
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

API_KEY = "cc_live_test_key"
T0 = "2026-10-02T10:00:00.000Z"
LIVE = "https://www.clearcotelabs.test/live/bs_run1?t=tok&handoff=1"
RECORDING = b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes"

EVENTS = [
    {"seq": 1, "at": T0, "type": "session.started", "data": {"kind": "run"}},
    {"seq": 2, "at": "2026-10-02T10:00:01.000Z", "type": "navigation",
     "data": {"url": "https://example.com/", "title": "Example Domain"}},
    {"seq": 3, "at": "2026-10-02T10:00:02.000Z", "type": "tab.closed", "data": {}},
    {"seq": 4, "at": "2026-10-02T10:00:03.000Z", "type": "session.ended", "data": {"reason": "run_finished"}},
]

SESSIONS = [
    {"id": "bs_a1", "status": "active", "worker": "w1", "createdAt": "2026-10-02T09:00:00.000Z",
     "costEur": 0.0123, "note": "crawler"},
    {"id": "bs_b2", "status": "ended", "worker": "w1", "createdAt": "2026-10-01T09:00:00.000Z",
     "costEur": 0.5, "note": None},
]

RESULT = {
    "status": "done", "detail": None, "url": "https://example.com/pricing", "title": "Pricing",
    "output": {"plan": "Starter", "price": "9.99"}, "outputError": None, "markdown": "# Pricing",
    "steps": [{"step": 1, "kind": "click", "action": "Open pricing", "text": None, "probability": 0.85,
               "decideMs": 361, "atMs": 1200, "url": "https://example.com/", "ledTo": "https://example.com/pricing",
               "pageChanged": True}],
    "usage": {"decision": {"inputTokens": 1200, "outputTokens": 30, "requests": 2},
              "text": {"inputTokens": 0, "outputTokens": 0, "requests": 0},
              "extract": {"inputTokens": 900, "outputTokens": 40, "requests": 1}, "estimatedUsd": 0.0006},
    "handoffs": 1, "elapsedMs": 14800, "jet": {"version": "0.1.0", "commit": "cd7011c"},
}


class FakeCloud:
    def __init__(self):
        self.log = []
        self.connect_url = "ws://127.0.0.1:9/devtools/browser/none"
        self.run_statuses = ["queued", "running", "waiting_for_human", "running", "succeeded"]
        self.flaky = 0            # answer the next N run GETs with 503
        self.handoff_polls = 2    # GET /browsers/{id} reads "waiting" this many times after a hand-off
        self.recording_state = "ready"
        self.recording_location = "/dev/blob/rec.mp4?sig=abc"  # relative, or another host entirely
        self.profile_cookies = {}
        self.webhooks = []
        self._n = 0
        self._run_gets = {}
        self._handoff = {}
        self._lock = threading.Lock()
        self.url = ""
        self.port = 0
        self._srv = None

    # -- helpers ---------------------------------------------------------------------------------
    def requests(self, method=None, path=None):
        return [r for r in self.log if (method is None or r["method"] == method)
                and (path is None or r["path"] == path)]

    def _session_view(self, sid, status="active"):
        h = self._handoff.get(sid)
        handoff = None
        if h is not None:
            state = "waiting" if h["polls"] > 0 else "done"
            handoff = {"state": state, "reason": h["reason"], "since": T0, "expiresAt": "2026-10-02T10:10:00.000Z",
                       "doneAt": None if state == "waiting" else "2026-10-02T10:01:00.000Z",
                       "liveUrl": LIVE if state == "waiting" else None}
            h["polls"] -= 1
        return {"id": sid, "status": status, "worker": "w1", "proxy": "managed", "note": None, "profile": None,
                "createdAt": T0, "startedAt": T0, "endedAt": None if status == "active" else T0,
                "endReason": None if status == "active" else "stopped:user", "stopRequested": status != "active",
                "usage": {"bytesUp": 100, "bytesDown": 2000, "gb": 0.0000021, "seconds": 12},
                "costEur": 0.0012, "pricing": {"eurPerGb": 1, "eurPerHour": 0}, "handoff": handoff,
                "recording": None}

    def _run_view(self, rid, status):
        waiting = status == "waiting_for_human"
        finished = status in ("succeeded", "failed", "cancelled", "expired")
        return {
            "id": rid, "status": status, "task": "Find the price", "url": "https://example.com/",
            "hasSchema": True, "createdAt": T0, "startedAt": None if status == "queued" else T0,
            "endedAt": T0 if finished else None,
            "result": RESULT if status == "succeeded" else None,
            "handoff": {"state": "waiting", "reason": "needs a login", "since": T0,
                        "expiresAt": "2026-10-02T10:10:00.000Z", "doneAt": None, "liveUrl": LIVE} if waiting else None,
            "session": self._session_view(rid, "ended" if finished else "active"),
            "costEur": {"browser": 0.0012, "agent": 0.0007, "total": 0.0019},
        }

    # -- routing ---------------------------------------------------------------------------------
    def handle(self, method, path, query, headers, body):
        if path == "/dev/blob/rec.mp4":
            return 200, RECORDING, {"content-type": "video/mp4"}
        if path == "/login-page":
            return 200, b"<html><title>login</title>signed in</html>", {
                "content-type": "text/html", "set-cookie": "sid=s3cret; Path=/; HttpOnly"}
        if headers.get("authorization") != f"Bearer {API_KEY}":
            return 401, {"error": "Missing or invalid API key."}, None
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if parts[:2] != ["api", "v1"]:
            return 404, {"error": "Not found."}, None
        rest = parts[2:]

        if rest == ["browsers"] and method == "POST":
            if body.get("keepAlive") is not None and not isinstance(body.get("keepAlive"), bool):
                return 400, {"error": "keepAlive must be true or false"}, None
            with self._lock:
                self._n += 1
                sid = f"bs_{self._n}"
            return 201, {"id": sid, "worker": "w1", "connectUrl": self.connect_url,
                         "expiresAt": "2026-10-02T11:00:00.000Z", "pricing": {"eurPerGb": 1, "eurPerHour": 0},
                         "limits": {"maxSeconds": 3600, "idleSeconds": 300}, "engine": {"version": "153.0", "revision": "r29", "pinned": False},
                         "warnings": [], **({"profile": body["profile"]} if isinstance(body.get("profile"), dict) else {})}, None
        if rest == ["browsers"] and method == "GET":
            return 200, {"balanceEur": 12.5, "sessions": SESSIONS}, None
        if rest[:2] == ["browsers", "profiles"]:
            return self._profiles(method, rest[2:], body)
        if rest[0] == "browsers" and len(rest) >= 2:
            sid, tail = rest[1], rest[2:]
            if not tail and method == "GET":
                return 200, self._session_view(sid), None
            if not tail and method == "DELETE":
                return 200, self._session_view(sid, "ended"), None
            if tail == ["live"]:
                return 200, {"viewUrl": f"wss://w1.example/live/{sid}", "expiresAt": T0,
                             "interactive": query.get("control") == ["1"]}, None
            if tail == ["share"]:
                return 200, {"url": f"https://www.clearcotelabs.test/{'replay' if body.get('recording') else 'live'}/{sid}?t=x",
                             "expiresAt": T0, "control": bool(body.get("control"))}, None
            if tail == ["handoff"] and method == "POST":
                self._handoff[sid] = {"reason": body.get("reason"), "polls": self.handoff_polls}
                return 200, {"state": "waiting", "reason": body.get("reason"), "since": T0,
                             "expiresAt": "2026-10-02T10:10:00.000Z", "liveUrl": LIVE}, None
            if tail == ["handoff", "done"] and method == "POST":
                if sid in self._handoff:
                    self._handoff[sid]["polls"] = 0
                return 200, {"state": "done", "reason": None, "since": T0, "expiresAt": T0,
                             "doneAt": "2026-10-02T10:01:00.000Z", "liveUrl": None}, None
            if tail == ["events"]:
                after = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["2"])[0])
                left = [e for e in EVENTS if e["seq"] > after]
                page = left[:limit]
                more = len(left) > len(page)
                return 200, {"events": page, "next": page[-1]["seq"] if more else None}, None
            if tail == ["recording"]:
                if sid == "bs_unrecorded":
                    return 404, {"error": "This session was not recorded.", "code": "NOT_FOUND"}, None
                if self.recording_state != "ready":
                    return 409, {"error": "The recording is still processing.", "code": "NOT_READY"}, None
                return 302, b"", {"location": self.recording_location}
        if rest == ["runs"] and method == "POST":
            if "keepAlive" in body:
                return 400, {"error": "keepAlive does not apply to runs"}, None
            return 201, {"id": "bs_run1", "status": "queued", "worker": "w1", "createdAt": T0,
                         "expiresAt": "2026-10-02T10:15:00.000Z",
                         "pricing": {"eurPerGb": 1, "eurPerHour": 0, "eurPerMTok": 0.05,
                                     "eurPerMTokTextIn": 0.3, "eurPerMTokTextOut": 1.2},
                         "limits": {"maxSeconds": 900, "idleSeconds": 300},
                         "engine": {"version": "153.0", "revision": "r29", "pinned": False}, "warnings": []}, None
        if rest == ["runs"] and method == "GET":
            return 200, {"runs": [{"id": "bs_run1", "status": "succeeded", "task": "Find the price",
                                   "createdAt": T0, "endedAt": T0, "resultStatus": "done", "costEur": 0.0019}]}, None
        if rest[0] == "runs" and len(rest) == 2:
            rid = rest[1]
            if method == "DELETE":
                return 200, self._run_view(rid, "cancelled"), None
            with self._lock:
                if self.flaky > 0:
                    self.flaky -= 1
                    return 503, b"upstream restarting", {"content-type": "text/plain"}
                n = self._run_gets.get(rid, 0)
                self._run_gets[rid] = n + 1
            return 200, self._run_view(rid, self.run_statuses[min(n, len(self.run_statuses) - 1)]), None
        if rest == ["webhooks"] and method == "POST":
            hook = {"id": f"wh_{len(self.webhooks) + 1}", "url": body["url"], "events": body.get("events") or [],
                    "description": body.get("description"), "createdAt": T0}
            self.webhooks.append(dict(hook, lastDelivery=None))
            return 201, dict(hook, secret="whsec_test_secret"), None
        if rest == ["webhooks"] and method == "GET":
            return 200, {"webhooks": self.webhooks or [
                {"id": "wh_9", "url": "https://hooks.example.com/cc", "events": ["run.finished"], "description": None,
                 "createdAt": T0, "lastDelivery": {"at": T0, "status": 200, "ok": True}},
                {"id": "wh_8", "url": "https://hooks.example.com/all", "events": [], "description": "all",
                 "createdAt": T0, "lastDelivery": None}]}, None
        if rest[0] == "webhooks" and len(rest) == 2 and method == "DELETE":
            return 200, {"ok": True}, None
        if rest[0] == "webhooks" and rest[2:] == ["test"]:
            return 200, {"ok": True, "status": 200}, None
        return 404, {"error": "Not found.", "code": "NOT_FOUND"}, None

    def _profiles(self, method, rest, body):
        if not rest:
            return 200, {"profiles": [{"name": "acct-1", "bytes": 2048, "cookies": 12, "storage": 0, "savedBy": None,
                                       "createdAt": T0, "updatedAt": T0, "inUse": False}]}, None
        name = rest[0]
        if rest[1:] == ["cookies"] and method == "PUT":
            if name == "busy":
                return 409, {"error": "A live session is saving to this profile.", "code": "PROFILE_IN_USE"}, None
            have = {} if body.get("mode") == "replace" else dict(self.profile_cookies.get(name, {}))
            for c in body.get("cookies") or []:
                have[(c["name"], c["domain"], c.get("path") or "/")] = c
            self.profile_cookies[name] = have
            domains = sorted({c["domain"].lstrip(".") for c in have.values()})[:50]
            return 200, {"name": name, "cookies": len(have), "imported": len(body.get("cookies") or []),
                         "domains": domains, "bytes": 1234, "updatedAt": T0}, None
        if not rest[1:] and method == "GET":
            have = self.profile_cookies.get(name, {})
            return 200, {"name": name, "cookies": len(have), "domains": sorted({k[1] for k in have}),
                         "bytes": 1234, "storage": 0, "updatedAt": T0}, None
        if not rest[1:] and method == "DELETE":
            return 200, {"ok": True}, None
        return 404, {"error": "Not found.", "code": "NOT_FOUND"}, None


def start_fake_cloud():
    api = FakeCloud()

    class H(BaseHTTPRequestHandler):
        def _do(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            u = urlsplit(self.path)
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            api.log.append({"method": self.command, "path": u.path, "query": u.query, "headers": headers,
                            "body": body if raw else None})
            status, payload, extra = api.handle(self.command, u.path, parse_qs(u.query), headers, body)
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            hdrs = {"content-type": "application/json", **(extra or {})}
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _do

        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    # a short poll interval: shutdown() waits for it, and every test stops a server
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    api._srv = srv
    api.port = srv.server_address[1]
    api.url = f"http://127.0.0.1:{api.port}"
    return api


def stop_fake_cloud(api):
    api._srv.shutdown()
    api._srv.server_close()
