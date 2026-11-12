"""Start the Telegram trading assistant.

    python scripts/run_telegram.py

Message your bot on Telegram and it shows a menu: swing trading, intraday
equity, options buying, futures and options selling. Pick one and it tells
you when to buy and sell. It never places an order.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see the README).
TRADE_CAPITAL sets the rupees it sizes trades from (default 20000).
Keep this running: on your laptop, or on a small cloud server for 24 hours.
Stop it with Ctrl+C.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

import _bootstrap
from tradebot.bot.telegram_bot import TradeAssistant, http_api
from tradebot.swing.service import SwingService

DATA = _bootstrap.ROOT / "data"


class Intraday:
    """Runs `run_bot.py --assist` as a separate process. Its alerts go to
    Telegram by themselves; if it stops, the reason is passed on."""

    def __init__(self, capital: float, tell):
        self.capital, self.tell = capital, tell
        self.proc: subprocess.Popen | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> tuple[bool, str]:
        if self.running():
            return True, "The intraday adviser is already running."
        if not os.environ.get("KITE_API_KEY") or not _bootstrap.SESSION_FILE.exists():
            return False, (
                "Intraday needs live prices, and those need Zerodha's paid Kite Connect API plan "
                "(check the current price at developers.kite.trade).\n\n"
                "To switch it on:\n"
                "1. Create a Kite Connect app and put KITE_API_KEY and KITE_API_SECRET in .env\n"
                "2. Each morning run: python scripts/kite_login.py\n"
                "3. Tap Intraday equity again\n\n"
                "Swing trading works without any of this.")
        log = open(DATA / "intraday.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(_bootstrap.ROOT / "scripts" / "run_bot.py"), "--assist", "--no-browser",
             "--capital", str(self.capital)], stdout=log, stderr=subprocess.STDOUT, cwd=_bootstrap.ROOT)
        threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()
        return True, ("Intraday adviser is starting. The first start of the week downloads two years of "
                      "candles and tests the strategies, which takes a while. You will get a message "
                      "for every buy and sell, or one message if no strategy passes the test.")

    def _watch(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        if code not in (0, -15):                         # -15 is our own stop
            tail = (DATA / "intraday.log").read_text(errors="replace").strip().splitlines()[-12:]
            self.tell("The intraday adviser stopped:\n" + "\n".join(tail))
        elif code == 0:
            self.tell("The intraday adviser has finished for the day.")

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()


def main() -> None:
    _bootstrap.load_env()
    token = _bootstrap.require_env("TELEGRAM_BOT_TOKEN")
    chat_id = _bootstrap.require_env("TELEGRAM_CHAT_ID")
    capital = float(os.environ.get("TRADE_CAPITAL", "20000"))
    DATA.mkdir(exist_ok=True)

    api = http_api(token)
    swing = SwingService(DATA / "swing", capital)
    intraday = Intraday(capital, tell=lambda text: api("sendMessage", {"chat_id": chat_id, "text": text[:4000]}))
    bot = TradeAssistant(api, chat_id, swing, capital, DATA / "assistant.json",
                         start_intraday=intraday.start, stop_intraday=intraday.stop,
                         intraday_running=intraday.running)
    print(f"Trading assistant running. Capital Rs {capital:,.0f}. Message your bot on Telegram. Ctrl+C to stop.")
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        intraday.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
