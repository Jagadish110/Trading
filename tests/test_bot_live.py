"""Real-order logic and the live loop, driven against a simulated Zerodha.

These prove the bot's own logic. They cannot prove that Zerodha's servers
behave like the simulation, which only a supervised first live run can.
"""
import inspect
from datetime import datetime, timedelta

import pandas as pd
import pytest

from conftest import Scripted, make_day
from fake_kite import FakeKite
from tradebot.bot.alerts import TelegramAlerts
from tradebot.bot.broker import KiteBroker, OrderError, PaperBroker, Position
from tradebot.bot.engine import TradingEngine
from tradebot.bot.kite import KiteFeed
from tradebot.bot.runner import LiveRunner
from tradebot.bot.store import Store
from tradebot.config import CostConfig, RiskConfig

RISK = RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_position_pct=100.0)


def broker_and_kite(**kw):
    kite = FakeKite()
    kite.price["SBIN"] = 100.0
    return KiteBroker(kite, {"SBIN": 0.05}, sleep=lambda s: None, **kw), kite


def test_fake_matches_the_real_kiteconnect_signatures():
    kiteconnect = pytest.importorskip("kiteconnect")
    for name in ("place_order", "cancel_order", "order_history", "positions", "ltp",
                 "historical_data", "instruments", "profile"):
        real = inspect.signature(getattr(kiteconnect.KiteConnect, name))
        fake = inspect.signature(getattr(FakeKite, name))
        assert list(fake.parameters) == list(real.parameters), name


def test_entry_fills_and_stop_loss_goes_to_the_exchange():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.02)
    assert (fill.qty, fill.price) == (50, 100.0)
    entry, stop = kite.placed
    assert (entry["transaction_type"], entry["order_type"], entry["price"]) == ("BUY", "LIMIT", 100.1)
    assert (stop["transaction_type"], stop["order_type"], stop["quantity"]) == ("SELL", "SL-M", 50)
    assert stop["trigger_price"] == 98.0                 # rounded to the 5 paise tick
    assert kite.orders_by_id[fill.stop_order_id]["status"] == "TRIGGER PENDING"


def test_short_entry_mirrors_long():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", -1, 50, 100.0, 102.0)
    entry, stop = kite.placed
    assert (entry["transaction_type"], entry["price"]) == ("SELL", 99.9)
    assert (stop["transaction_type"], stop["trigger_price"]) == ("BUY", 102.0)
    assert fill.qty == 50


def test_unfilled_entry_is_cancelled_and_no_stop_is_placed():
    broker, kite = broker_and_kite()
    kite.never_fill.add("LIMIT")
    assert broker.enter("SBIN", 1, 50, 100.0, 98.0) is None
    assert len(kite.placed) == 1
    assert kite.orders_by_id["1"]["status"] == "CANCELLED"


def test_rejected_entry_returns_nothing():
    broker, kite = broker_and_kite()
    kite.reject.add("LIMIT")
    assert broker.enter("SBIN", 1, 50, 100.0, 98.0) is None
    assert len(kite.placed) == 1


def test_position_is_closed_at_once_if_the_stop_order_is_refused():
    broker, kite = broker_and_kite()
    kite.reject.add("SL-M")
    assert broker.enter("SBIN", 1, 50, 100.0, 98.0) is None
    assert [o["order_type"] for o in kite.placed] == ["LIMIT", "SL-M", "MARKET"]
    assert kite.placed[-1]["transaction_type"] == "SELL"
    assert kite.positions()["net"][0]["quantity"] == 0   # flat again


def test_exchange_stop_fill_is_picked_up():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    assert broker.stop_fill(pos, 99.0, 99.0, 99.0) is None
    kite.move("SBIN", 97.8)
    assert broker.stop_fill(pos, 97.8, 97.8, 97.8) == 97.8


def test_exit_cancels_the_stop_then_sells():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    kite.move("SBIN", 105.0)
    assert broker.exit(pos, 105.0, "target") == (105.0, "target")
    assert kite.orders_by_id[fill.stop_order_id]["status"] == "CANCELLED"
    assert kite.placed[-1]["order_type"] == "MARKET" and kite.placed[-1]["quantity"] == 50
    assert kite.positions()["net"][0]["quantity"] == 0


def test_exit_does_not_double_sell_when_the_stop_filled_first():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    kite.move("SBIN", 97.5)                              # stop fills at the exchange
    assert broker.exit(pos, 97.5, "square_off") == (97.5, "stop")
    assert [o["order_type"] for o in kite.placed] == ["LIMIT", "SL-M"]     # no extra order
    assert kite.positions()["net"][0]["quantity"] == 0


def test_failed_exit_raises_loudly():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    kite.reject.add("MARKET")
    with pytest.raises(OrderError, match="CHECK ZERODHA"):
        broker.exit(pos, 101.0, "manual")


def test_mismatch_with_zerodha_is_reported():
    broker, kite = broker_and_kite()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    assert broker.problems([pos]) == []
    assert broker.problems([]) == ["SBIN: bot expects 0 shares, the broker shows 50"]


# -- engine on top of the live broker ----------------------------------
def live_engine(kite, alerts=None, store=None):
    broker = KiteBroker(kite, sleep=lambda s: None)
    return TradingEngine(Scripted({}), broker, store, RISK, alerts=alerts)


def test_engine_round_trip_through_the_live_broker():
    kite = FakeKite()
    kite.price["SBIN"] = 100.0
    engine = live_engine(kite)
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("SBIN", t, 1, 98.0, 105.0, False, False)
    engine.on_open("SBIN", t + pd.Timedelta(minutes=5), 100.0)
    assert engine.mode == "live" and engine.positions["SBIN"].qty == 487
    assert engine.store.open_positions("live")[0]["stop_order_id"] == "2"
    kite.move("SBIN", 97.9)
    engine.on_tick("SBIN", t + pd.Timedelta(minutes=6), 97.9)
    trade = engine.trades[0]
    assert (trade["reason"], trade["exit_price"]) == ("stop", 97.9)
    assert len(kite.placed) == 2                         # entry and stop only: the exchange did the exit
    assert kite.positions()["net"][0]["quantity"] == 0


def test_engine_pauses_and_alerts_when_an_exit_fails():
    sent = []
    alerts = TelegramAlerts("TOKEN", "42", post=lambda url, payload: sent.append(payload["text"]))
    kite = FakeKite()
    kite.price["SBIN"] = 100.0
    engine = live_engine(kite, alerts)
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("SBIN", t, 1, 98.0, 105.0, False, False)
    engine.on_open("SBIN", t + pd.Timedelta(minutes=5), 100.0)
    kite.reject.add("MARKET")
    kite.move("SBIN", 105.0)
    engine.on_tick("SBIN", t + pd.Timedelta(minutes=6), 105.0)
    alerts.close()
    assert "SBIN" in engine.positions                    # still tracked, not silently dropped
    assert engine.status == "paused" and engine.risk.halted
    assert any("CHECK ZERODHA" in m for m in sent)
    assert any(m.startswith("BUY SBIN 487") for m in sent)


def test_restart_pauses_if_zerodha_disagrees(tmp_path):
    kite = FakeKite()
    kite.price["SBIN"] = 100.0
    store = Store(tmp_path / "bot.db")
    engine = live_engine(kite, store=store)
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("SBIN", t, 1, 98.0, 105.0, False, False)
    engine.on_open("SBIN", t + pd.Timedelta(minutes=5), 100.0)

    same = live_engine(kite, store=store)
    same.restore(today=t.date())
    assert same.status == "running" and "SBIN" in same.positions

    kite.move("SBIN", 97.0)                              # stopped out while the bot was off
    after = live_engine(kite, store=store)
    after.restore(today=t.date())
    assert after.status == "paused" and "do not match" in after.note


# -- the live loop -------------------------------------------------------
class Clock:
    def __init__(self, start):
        self.t = pd.Timestamp(start).to_pydatetime()

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def day_with_a_rally():
    prev = make_day("2026-01-02", [100.0] * 75)
    today = make_day("2026-01-05", [100.0] * 75)
    today.loc[pd.Timestamp("2026-01-05 10:20"):, ["open", "high", "low", "close"]] = [106.0, 106.5, 105.5, 106.0]
    today.loc[pd.Timestamp("2026-01-05 10:20"), ["open", "low"]] = [100.0, 99.5]
    return pd.concat([prev, today])


@pytest.mark.parametrize("live", [False, True])
def test_live_loop_trades_a_whole_day(live):
    clock = Clock("2026-01-05 09:00:00")
    data = {"SBIN": day_with_a_rally(), "INFY": pd.concat([make_day("2026-01-02", [500.0] * 75),
                                                           make_day("2026-01-05", [500.0] * 75)])}
    kite = FakeKite(data, now=clock)
    feed = KiteFeed(kite, ["SBIN", "INFY"], sleep=lambda s: None)
    plan = {"2026-01-05 10:00": (1, 98.0, 105.0), "2026-01-05 13:00": (-1, 108.0, 90.0)}
    strategy = Scripted(plan)
    strategy.signals_orig = strategy.signals
    # Only SBIN trades; INFY rides along to prove quiet symbols are harmless.
    strategy.signals = lambda df: strategy.signals_orig(df) if df["close"].iloc[-1] < 300 else Scripted.empty(df)
    broker = KiteBroker(kite, feed.tick_sizes, sleep=lambda s: None) if live else PaperBroker(CostConfig())
    sent = []
    alerts = TelegramAlerts("T", "1", post=lambda url, payload: sent.append(payload["text"]))
    engine = TradingEngine(strategy, broker, risk=RISK, alerts=alerts)
    runner = LiveRunner(engine, feed, ["SBIN", "INFY"], now=clock, sleep=clock.sleep)
    runner.run()
    alerts.close()

    assert clock.t >= datetime(2026, 1, 5, 15, 30)
    first, second = engine.trades
    # Signal on the 10:00 candle, acted on a few seconds after it closes at 10:05.
    assert first["entry_time"].floor("min") == pd.Timestamp("2026-01-05 10:05")
    assert (first["side"], first["reason"]) == (1, "target")
    assert first["exit_time"].floor("5min") == pd.Timestamp("2026-01-05 10:20")
    assert (second["side"], second["reason"]) == (-1, "square_off")
    assert second["exit_time"].floor("min") == pd.Timestamp("2026-01-05 15:15")
    assert engine.positions == {}
    assert any(m.startswith("Day over") and "2 trades" in m for m in sent)
    if live:
        assert kite.positions()["net"][0]["quantity"] == 0
        types = [o["order_type"] for o in kite.placed]
        assert types == ["LIMIT", "SL-M", "MARKET", "LIMIT", "SL-M", "MARKET"]
        assert all(o["status"] in ("COMPLETE", "CANCELLED") for o in kite.orders_by_id.values())


def test_loop_does_nothing_before_the_open_or_at_weekends():
    for start in ("2026-01-05 08:00:00", "2026-01-03 11:00:00"):      # early Monday, Saturday
        clock = Clock(start)
        kite = FakeKite({"SBIN": day_with_a_rally()}, now=clock)
        feed = KiteFeed(kite, ["SBIN"], sleep=lambda s: None)
        engine = TradingEngine(Scripted({}), PaperBroker(), risk=RISK)
        runner = LiveRunner(engine, feed, ["SBIN"], now=clock, sleep=clock.sleep)
        assert runner.step() == "waiting"


def test_unknown_symbol_is_refused_up_front():
    kite = FakeKite({"SBIN": day_with_a_rally()})
    with pytest.raises(RuntimeError, match="NOPE"):
        KiteFeed(kite, ["SBIN", "NOPE"])


def test_telegram_failure_never_raises():
    def boom(url, payload):
        raise OSError("network down")
    alerts = TelegramAlerts("TOKEN", "42", post=boom)
    alerts.send("hello")
    alerts.close()
    assert alerts.failures == 1


def test_telegram_message_shape():
    seen = []
    alerts = TelegramAlerts("TOKEN", "42", prefix="[paper] ", post=lambda url, p: seen.append((url, p)))
    alerts.send("BUY SBIN")
    alerts.close()
    assert seen == [("https://api.telegram.org/botTOKEN/sendMessage", {"chat_id": "42", "text": "[paper] BUY SBIN"})]
