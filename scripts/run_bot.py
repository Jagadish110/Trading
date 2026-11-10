"""Start the trading bot and its dashboard.

    python scripts/run_bot.py --strategy orb --replay data/sample   # demo on saved candles, no broker
    python scripts/run_bot.py --strategy orb                        # paper trading on live prices
    python scripts/run_bot.py --strategy orb --live                 # REAL orders, real money

Paper trading is the default. It uses live Zerodha prices but no order
ever leaves the laptop. Real trading needs three things: the --live flag,
LIVE_TRADING=yes in .env, and a typed confirmation at start-up.

Stop the bot with Ctrl+C. Open positions are saved and picked up again on
the next start the same day. In live mode their stop-losses stay at the
exchange while the bot is off.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from dataclasses import replace

import _bootstrap
from tradebot.bot import alerts as alerts_mod
from tradebot.bot.broker import KiteBroker, PaperBroker
from tradebot.bot.dashboard import Dashboard
from tradebot.bot.engine import EngineConfig, TradingEngine
from tradebot.bot.runner import LiveRunner, replay
from tradebot.bot.store import Store
from tradebot.config import DEFAULT_WATCHLIST, CostConfig, RiskConfig
from tradebot.strategies import REGISTRY

CONFIRM = "TRADE REAL MONEY"


def parse_params(pairs: list[str], defaults: dict) -> dict:
    """--param rr=3 --param or_minutes=30, converted to each setting's own type."""
    out = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key not in defaults:
            sys.exit(f"Unknown setting '{key}'. This strategy has: {', '.join(defaults)}")
        out[key] = type(defaults[key])(value)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", required=True, choices=list(REGISTRY))
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                    help="strategy setting, e.g. --param rr=3 (repeatable)")
    ap.add_argument("--symbols", nargs="*", default=None, help="default: the watchlist in config.py")
    ap.add_argument("--capital", type=float, default=RiskConfig.capital)
    ap.add_argument("--risk-pct", type=float, default=RiskConfig.risk_per_trade_pct)
    ap.add_argument("--no-short", action="store_true", help="long trades only")
    ap.add_argument("--live", action="store_true", help="place REAL orders at Zerodha")
    ap.add_argument("--replay", metavar="FOLDER", help="run saved candles instead of live prices")
    ap.add_argument("--pace", type=float, default=0.05, help="replay: seconds per candle (0 = flat out)")
    ap.add_argument("--port", type=int, default=8765, help="dashboard port")
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    ap.add_argument("--db", default=None, help="database file (default: data/tradebot.db)")
    args = ap.parse_args()
    if args.live and args.replay:
        sys.exit("--live and --replay cannot be combined.")

    _bootstrap.load_env()
    cls = REGISTRY[args.strategy]
    strategy = cls(**parse_params(args.param, cls.default_params))
    risk = replace(RiskConfig(), capital=args.capital, risk_per_trade_pct=args.risk_pct)
    costs = CostConfig()
    cfg = EngineConfig(allow_short=not args.no_short)
    mode = "replay" if args.replay else "live" if args.live else "paper"

    if mode == "live":
        if os.environ.get("LIVE_TRADING", "").lower() != "yes":
            sys.exit("Live trading is switched off. To allow it, add LIVE_TRADING=yes to .env.")
        print(f"\nYou are about to trade REAL MONEY at Zerodha.\n"
              f"  strategy       {strategy!r}\n"
              f"  capital        Rs {risk.capital:,.0f}\n"
              f"  risk per trade {risk.risk_per_trade_pct}%  (about Rs {risk.capital * risk.risk_per_trade_pct / 100:,.0f})\n"
              f"  daily loss cap {risk.max_daily_loss_pct}%  (about Rs {risk.capital * risk.max_daily_loss_pct / 100:,.0f})\n")
        if input(f'Type "{CONFIRM}" to continue: ').strip() != CONFIRM:
            sys.exit("Not confirmed. Nothing was started.")

    if mode == "replay":
        db_path = args.db or _bootstrap.ROOT / "data" / "replay.db"
        if not args.db and db_path.exists():
            db_path.unlink()                             # every replay starts clean
    else:
        db_path = args.db or _bootstrap.ROOT / "data" / "tradebot.db"
    store = Store(db_path)
    alerts = alerts_mod.NullAlerts() if mode == "replay" else alerts_mod.from_env(
        os.environ, prefix="" if mode == "live" else "[paper] ")

    if mode == "replay":
        from tradebot.data import load_folder
        data = load_folder(args.replay, args.symbols)
        engine = TradingEngine(strategy, PaperBroker(costs), store, risk, costs=costs, cfg=cfg, mode="replay")
        work = lambda: replay(engine, data, pace=args.pace, should_stop=lambda: stopping.is_set())
    else:
        from tradebot.bot.kite import KiteFeed, connect, ist_now
        try:
            kite = connect(_bootstrap.require_env("KITE_API_KEY"), _bootstrap.SESSION_FILE)
            symbols = args.symbols or DEFAULT_WATCHLIST
            feed = KiteFeed(kite, symbols)
        except RuntimeError as e:
            sys.exit(str(e))
        broker = KiteBroker(kite, feed.tick_sizes, log=store.log) if mode == "live" else PaperBroker(costs)
        engine = TradingEngine(strategy, broker, store, risk, costs=costs, cfg=cfg, alerts=alerts)
        engine.restore(ist_now().date())
        runner = LiveRunner(engine, feed, symbols)
        work = runner.run

    stopping = threading.Event()
    dash = Dashboard(engine, port=args.port).start()
    print(f"\nMode: {mode.upper()}   Strategy: {strategy!r}")
    print(f"Dashboard: {dash.url}")
    print(f"Telegram alerts: {'on' if alerts.enabled else 'off'}")
    print("Press Ctrl+C to stop.\n")
    if not args.no_browser:
        webbrowser.open(dash.url)
    engine.log("info", f"Bot started in {mode} mode with {strategy!r}", alert=True)

    try:
        work()
        if mode == "replay":
            t = store.totals("replay")
            print(f"Replay finished: {t['trades']} trades, net Rs {t['net']:,.0f}. "
                  "The dashboard stays open until you press Ctrl+C.")
            stopping.wait()
        else:
            print("Trading day is over.")
    except KeyboardInterrupt:
        stopping.set()
        if mode != "replay":
            runner.stop_requested = True
        if engine.positions:
            print(f"\nStopped with {len(engine.positions)} position(s) still open: "
                  f"{', '.join(engine.positions)}.")
            print("Their stop-losses stay at the exchange." if mode == "live"
                  else "They are saved and will resume if you restart today.")
        engine.log("warn", "Bot stopped by you", alert=True)
    finally:
        alerts.close()
        dash.stop()
        store.close()


if __name__ == "__main__":
    main()
