"""Telegram alerts to your phone.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see the README). Sending
happens on a background thread and a failure is never allowed to disturb
trading: it is only counted and printed.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.request


class NullAlerts:
    """Used when Telegram is not set up."""
    enabled = False

    def send(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


class TelegramAlerts:
    enabled = True
    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, prefix: str = "", post=None, timeout: float = 10.0):
        self.chat_id = chat_id
        self.prefix = prefix
        self.failures = 0
        self._url = self.API.format(token=token)
        self._timeout = timeout
        self._post = post or self._http_post
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="telegram")
        self._thread.start()

    def send(self, text: str) -> None:
        try:
            self._queue.put_nowait(self.prefix + text)
        except queue.Full:                               # never block the trading loop
            self.failures += 1

    def close(self, wait: float = 5.0) -> None:
        """Let queued messages go out, then stop the worker."""
        self._queue.put(None)
        self._thread.join(wait)

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                self._post(self._url, {"chat_id": self.chat_id, "text": text})
            except Exception as e:
                self.failures += 1
                # The URL holds the bot token, so only the error type is printed.
                print(f"Telegram alert failed ({type(e).__name__})", file=sys.stderr)

    def _http_post(self, url: str, payload: dict) -> None:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            resp.read()


def from_env(env: dict, prefix: str = ""):
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return TelegramAlerts(token, chat, prefix)
    return NullAlerts()
