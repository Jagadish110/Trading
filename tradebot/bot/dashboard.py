"""The dashboard: a small web page served from the laptop to the laptop.

It listens on 127.0.0.1 only, so nothing outside this computer can reach
it, and every request must carry a random token created at start-up.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("dashboard.html")


class Dashboard:
    def __init__(self, engine, port: int = 8765, host: str = "127.0.0.1"):
        self.engine = engine
        self.token = secrets.token_urlsafe(24)
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):                # keep the terminal quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, default=str).encode(), "application/json")

            def _allowed(self) -> bool:
                # Local browser only: right host name and the start-up token.
                host_name = (self.headers.get("Host") or "").rsplit(":", 1)[0]
                given = self.headers.get("X-Token") or ""
                return host_name in ("127.0.0.1", "localhost") and secrets.compare_digest(given, dash.token)

            def do_GET(self):
                if self.path.split("?")[0] == "/":
                    html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", dash.token)
                    self._send(200, html.encode(), "text/html; charset=utf-8")
                elif self.path == "/api/state":
                    if not self._allowed():
                        return self._json(403, {"error": "forbidden"})
                    self._json(200, dash.engine.snapshot())
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._allowed():
                    return self._json(403, {"error": "forbidden"})
                actions = {"/api/pause": dash.engine.pause, "/api/resume": dash.engine.resume,
                           "/api/kill": dash.engine.kill}
                action = actions.get(self.path)
                if action is None:
                    return self._json(404, {"error": "not found"})
                action()
                self._json(200, dash.engine.snapshot())

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.url = f"http://{host}:{self.server.server_address[1]}/"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="dashboard")

    def start(self) -> "Dashboard":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
