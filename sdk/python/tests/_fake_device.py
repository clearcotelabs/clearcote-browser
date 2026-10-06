"""A local stand-in for the site's device-login endpoints, implementing the contract the server side
is built to (and every branch of it). Mirrors sdk/node/test/helpers/fake-device.ts.

    POST /api/v1/device/code   {"client_name", "client_version"}  -> 200 {device_code, user_code, ...}
                                anything else                       -> 400 {"error": "invalid_request"}
    POST /api/v1/device/token  {"device_code"}                     -> the next scripted answer:
        "pending" -> 400 authorization_pending     "slow"    -> 400 slow_down
        "expired" -> 400 expired_token             "denied"  -> 400 access_denied
        "invalid" -> 400 invalid_request           "429"     -> 429 (rate limited)
        "500"     -> 500                           "drop"    -> connection closed, no answer
        "ok"      -> 200 {license_key, plan, expires_at}
      A wrong device_code is answered 400 invalid_request whatever the script says.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEVICE_CODE = "dc_fake_device_code_0001"
USER_CODE = "WDJB-MJHT"
LICENSE_KEY = "cc_lic_devicelogin_fake_key_7Q2x9"

_ERRORS = {"pending": "authorization_pending", "slow": "slow_down", "expired": "expired_token",
           "denied": "access_denied", "invalid": "invalid_request"}


class FakeDevice:
    def __init__(self, script, plan="pro", expires_at="2027-01-31T00:00:00.000Z", interval=5,
                 expires_in=900, code_status=200, retry_after=None):
        self.script = list(script)
        self.plan = plan
        self.expires_at = expires_at
        self.interval = interval
        self.expires_in = expires_in
        self.code_status = code_status
        self.retry_after = retry_after
        self.log = []
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n).decode() if n else ""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = None
                fake.log.append({"path": self.path, "body": body,
                                 "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path == "/api/v1/device/code":
                    if fake.code_status != 200:
                        return self._send(fake.code_status, {"error": "not_found"})
                    if not isinstance(body, dict) or not body.get("client_name") or not body.get("client_version"):
                        return self._send(400, {"error": "invalid_request"})
                    base = f"http://127.0.0.1:{fake.port}"
                    return self._send(200, {
                        "device_code": DEVICE_CODE, "user_code": USER_CODE,
                        "verification_uri": f"{base}/device",
                        "verification_uri_complete": f"{base}/device?code={USER_CODE}",
                        "expires_in": fake.expires_in, "interval": fake.interval})
                if self.path == "/api/v1/device/token":
                    if not isinstance(body, dict) or body.get("device_code") != DEVICE_CODE:
                        return self._send(400, {"error": "invalid_request"})
                    step = fake.script.pop(0) if fake.script else "pending"
                    if step == "drop":
                        self.close_connection = True
                        return None  # no status line at all: the client sees the connection close
                    if step == "ok":
                        return self._send(200, {"license_key": LICENSE_KEY, "plan": fake.plan,
                                                "expires_at": fake.expires_at})
                    if step == "429":
                        return self._send(429, {"error": "Rate limit exceeded."},
                                          {"retry-after": str(fake.retry_after)} if fake.retry_after else None)
                    if step == "500":
                        return self._send(500, {"error": "internal"})
                    return self._send(400, {"error": _ERRORS[step]})
                return self._send(404, {"error": "not found"})

            def log_message(self, *_a):
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def token_polls(self):
        return [e for e in self.log if e["path"] == "/api/v1/device/token"]

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()
