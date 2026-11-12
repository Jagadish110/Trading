"""The Telegram menu: message the bot, pick one of four ways to trade, and
it tells you when to buy and sell. It never places an order.

Only the chat whose id is configured is answered. Anyone else who finds
the bot gets silence.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

from .kite import ist_now

MENU = [
    ("swing", "Swing trading (days)"),
    ("intraday", "Intraday equity (same day)"),
    ("options", "Options buying"),
    ("fno_sell", "Futures & options selling"),
    ("status", "Status"),
    ("stop", "Stop alerts"),
]
MENU_TEXT = ("What do you want help with? I only advise; you place every order yourself.\n"
             "No strategy here is proven on real money. Start small.")
SCAN_AT = (16, 15)                       # daily swing scan, IST, Monday to Friday

# Rough figures for one lot of an index option. They change (lot sizes are
# revised, premiums move every minute), so treat the replies as estimates.
NIFTY_LOT = 65
TYPICAL_PREMIUM = 150.0                  # rupees per share for a near-the-money weekly option
PREMIUM_STOP_PCT = 30.0                  # a common exit rule: get out after losing this much of the premium
SELL_MARGIN = 175_000.0                  # margin for selling one index option or future


def options_buying_check(capital: float, risk_pct: float = 1.0) -> tuple[bool, str]:
    cost = NIFTY_LOT * TYPICAL_PREMIUM
    loss = cost * PREMIUM_STOP_PCT / 100
    limit = capital * risk_pct / 100
    need = loss / (risk_pct / 100)
    if loss <= limit and cost <= capital * 0.3:
        return True, ("Your capital is large enough for one lot within the risk limit, but the options "
                      "strategy has not been built or tested yet, so I have no signals to give.")
    return False, (
        "Options buying is blocked at your capital.\n\n"
        f"• One Nifty lot ({NIFTY_LOT} shares) at a premium near Rs {TYPICAL_PREMIUM:.0f} costs about "
        f"Rs {cost:,.0f}. That is {cost / capital:.0%} of your Rs {capital:,.0f} in one trade.\n"
        f"• Even exiting after a {PREMIUM_STOP_PCT:.0f}% fall in premium loses about Rs {loss:,.0f}. "
        f"Your limit is {risk_pct:g}% per trade, which is Rs {limit:,.0f}.\n"
        f"• To take that trade inside the limit you would need about Rs {need:,.0f}.\n\n"
        "About 9 in 10 individual traders lose money in F&O (SEBI study, FY22 to FY24). "
        "Swing trading and intraday equity are open to you now.")


def fno_selling_check(capital: float) -> tuple[bool, str]:
    if capital >= SELL_MARGIN * 2:
        return True, ("Your capital covers the margin, but the selling strategy has not been built or "
                      "tested yet, so I have no signals to give.")
    return False, (
        "Futures and options selling is not possible at your capital.\n\n"
        f"• The exchange asks for about Rs {SELL_MARGIN:,.0f} of margin to sell one index option or future. "
        f"You have Rs {capital:,.0f}, so the broker will reject the order.\n"
        "• Selling has small regular gains and rare losses many times larger than a gain, so a "
        f"cushion is needed on top of the margin: about Rs {SELL_MARGIN * 2:,.0f} in total.\n\n"
        "Swing trading and intraday equity are open to you now.")


def http_api(token: str, timeout: float = 40.0):
    """Returns call(method, payload) -> result for the Telegram Bot API."""
    base = f"https://api.telegram.org/bot{token}/"

    def call(method: str, payload: dict):
        req = urllib.request.Request(base + method, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()).get("result")
    return call


class TradeAssistant:
    """`api(method, payload)` talks to Telegram. `swing` is a SwingService.
    `start_intraday()` starts the intraday adviser and returns (ok, message);
    `stop_intraday()` stops it and `intraday_running()` reports on it."""

    def __init__(self, api, chat_id, swing, capital: float, state_path: str | Path,
                 start_intraday=None, stop_intraday=None, intraday_running=None,
                 now=ist_now, background: bool = True):
        self.api, self.chat_id = api, str(chat_id)
        self.swing, self.capital = swing, capital
        self.state_path = Path(state_path)
        self.start_intraday = start_intraday or (lambda: (False, "Intraday is not set up on this machine."))
        self.stop_intraday = stop_intraday or (lambda: None)
        self.intraday_running = intraday_running or (lambda: False)
        self.now, self.background = now, background
        self.offset = 0
        self.stop_requested = False
        self._scan_lock = threading.Lock()
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("swing_on", False)
        self.state.setdefault("last_scan", None)

    # -- plumbing ----------------------------------------------------------
    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state))

    def send(self, text: str, menu: bool = False) -> None:
        payload = {"chat_id": self.chat_id, "text": text[:4000]}
        if menu:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": key}] for key, label in MENU]}
        self.api("sendMessage", payload)

    def _spawn(self, fn) -> None:
        if self.background:
            threading.Thread(target=fn, daemon=True).start()
        else:
            fn()

    # -- incoming ------------------------------------------------------------
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            q = update["callback_query"]
            chat = str(q.get("message", {}).get("chat", {}).get("id", ""))
            self.api("answerCallbackQuery", {"callback_query_id": q["id"]})
            if chat == self.chat_id:
                self.choose(q.get("data", ""))
            return
        msg = update.get("message") or {}
        if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
            return                                       # not the owner: say nothing
        text = (msg.get("text") or "").strip()
        word, _, rest = text.partition(" ")
        word = word.lower().split("@")[0]
        if word == "/skip" and rest.strip():
            self.send(self.swing.skip(rest.strip()))
        elif word in ("/status", "/stop", "/swing", "/intraday"):
            self.choose(word[1:])
        else:
            self.send(MENU_TEXT, menu=True)

    def choose(self, key: str) -> None:
        if key == "swing":
            self.state["swing_on"] = True
            self._save()
            self.send("Swing trading is on. Scanning now; the first scan downloads prices and tests the "
                      f"strategies, which can take a few minutes. After this I scan every weekday at "
                      f"{SCAN_AT[0]}:{SCAN_AT[1]:02d} and message you.")
            self._spawn(self.scan)
        elif key == "intraday":
            ok, message = self.start_intraday()
            self.send(message)
        elif key == "options":
            self.send(options_buying_check(self.capital)[1])
        elif key == "fno_sell":
            self.send(fno_selling_check(self.capital)[1])
        elif key == "status":
            self.send("\n".join([
                f"Capital setting: Rs {self.capital:,.0f}",
                f"Swing alerts: {'on' if self.state['swing_on'] else 'off'}"
                + (f" (last scan {self.state['last_scan']})" if self.state["last_scan"] else ""),
                f"Intraday adviser: {'running' if self.intraday_running() else 'not running'}",
                "", self.swing.holdings()]))
        elif key == "stop":
            self.state["swing_on"] = False
            self._save()
            self.stop_intraday()
            self.send("Stopped. No more alerts until you pick an option again. "
                      "Anything you are holding is still yours to manage, and its GTT orders stay active.")
        else:
            self.send(MENU_TEXT, menu=True)

    # -- swing scan ----------------------------------------------------------
    def scan(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return                                       # one already running
        try:
            text = self.swing.run()
            self.state["last_scan"] = str(self.now().date())
            self._save()
        except Exception as e:
            text = f"The swing scan failed ({type(e).__name__}: {e}). I will try again at the next scan."
        finally:
            self._scan_lock.release()
        self.send(text)

    def tick(self) -> None:
        """Called between polls: run the daily scan when it is due."""
        now = self.now()
        due = (self.state["swing_on"] and now.weekday() < 5 and (now.hour, now.minute) >= SCAN_AT
               and self.state["last_scan"] != str(now.date()))
        if due:
            self.state["last_scan"] = str(now.date())    # claim it so it is not started twice
            self._save()
            self._spawn(self.scan)

    # -- main loop -----------------------------------------------------------
    def poll_once(self, timeout: int = 25) -> None:
        updates = self.api("getUpdates", {"offset": self.offset, "timeout": timeout,
                                          "allowed_updates": ["message", "callback_query"]}) or []
        for u in updates:
            self.offset = max(self.offset, u["update_id"] + 1)
            try:
                self.handle(u)
            except Exception as e:                       # one bad message must not stop the bot
                print(f"Could not handle a message ({type(e).__name__}: {e})")

    def run(self, sleep=time.sleep) -> None:
        self.send("Trading assistant is online.\n" + MENU_TEXT, menu=True)
        failures = 0
        while not self.stop_requested:
            try:
                self.poll_once()
                self.tick()
                failures = 0
            except Exception as e:
                failures += 1
                if failures in (1, 30):
                    print(f"Telegram is unreachable ({type(e).__name__}). Retrying.")
                sleep(min(60, 2 * failures))
