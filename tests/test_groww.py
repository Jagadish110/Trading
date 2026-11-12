"""Groww support: the adapter that lets the Zerodha-shaped broker, feed and
history download run on a Groww account."""
import ast
import importlib.util
import inspect
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from conftest import Scripted, make_day
from fake_groww import FakeGroww
from tradebot.bot.broker import KiteBroker, Position
from tradebot.bot.engine import TradingEngine
from tradebot.bot.groww import GrowwAsKite, connect, totp
from tradebot.bot.history import download
from tradebot.bot.kite import KiteFeed
from tradebot.config import RiskConfig
from tradebot.data import load_folder, make_synthetic

RISK = RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_position_pct=100.0)


def account(price=100.0):
    g = FakeGroww()
    g.price["SBIN"] = price
    kite = GrowwAsKite(g, sleep=lambda s: None)
    return KiteBroker(kite, {"SBIN": 0.05}, sleep=lambda s: None), g


def test_fake_matches_the_real_growwapi_signatures():
    spec = importlib.util.find_spec("growwapi")
    if spec is None:
        pytest.skip("growwapi is not installed")
    source = (Path(spec.origin).parent / "groww" / "client.py").read_text()
    real = {n.name: [a.arg for a in n.args.args]
            for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}
    for name in ("place_order", "cancel_order", "get_order_detail", "get_ltp", "get_positions_for_user",
                 "get_historical_candle_data", "get_user_profile", "get_all_instruments"):
        assert list(inspect.signature(getattr(FakeGroww, name)).parameters) == real[name], name


def test_totp_matches_the_published_test_vector():
    # RFC 6238 appendix B, SHA-1, secret "12345678901234567890"
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp(secret, at=59) == "287082"
    assert totp(secret, at=1111111109) == "081804"


def test_entry_and_exchange_stop_loss_on_groww():
    broker, g = account()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.02)
    assert (fill.qty, fill.price) == (50, 100.0)
    entry, stop = g.placed
    assert (entry["transaction_type"], entry["order_type"], entry["price"]) == ("BUY", "LIMIT", 100.1)
    assert (stop["transaction_type"], stop["order_type"], stop["trigger_price"]) == ("SELL", "SL_M", 98.0)
    assert g.orders[fill.stop_order_id]["order_status"] == "TRIGGER_PENDING"


def test_stop_fill_target_exit_and_no_double_sell_on_groww():
    broker, g = account()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    assert broker.stop_fill(pos, 99, 99, 99) is None
    g.move("SBIN", 105.0)
    assert broker.exit(pos, 105.0, "target") == (105.0, "target")
    assert g.orders[fill.stop_order_id]["order_status"] == "CANCELLED"
    assert broker.problems([]) == []                       # flat at Groww

    broker, g = account()
    fill = broker.enter("SBIN", 1, 50, 100.0, 98.0)
    pos = Position("SBIN", 1, 50, None, 100.0, 98.0, 105.0, fill.stop_order_id)
    g.move("SBIN", 97.5)                                   # stop fills at the exchange first
    assert broker.exit(pos, 97.5, "square_off") == (97.5, "stop")
    assert [o["order_type"] for o in g.placed] == ["LIMIT", "SL_M"]
    assert broker.problems([pos]) == ["SBIN: bot expects 50 shares, the broker shows 0"]


def test_rejected_orders_on_groww():
    broker, g = account()
    g.reject.add("LIMIT")
    assert broker.enter("SBIN", 1, 50, 100.0, 98.0) is None
    broker, g = account()
    g.reject.add("SL_M")                                   # no stop possible: get out at once
    assert broker.enter("SBIN", 1, 50, 100.0, 98.0) is None
    assert [o["order_type"] for o in g.placed] == ["LIMIT", "SL_M", "MARKET"]
    assert g.get_positions_for_user()["positions"][0]["quantity"] == 0


def test_engine_round_trip_on_groww():
    g = FakeGroww()
    g.price["SBIN"] = 100.0
    engine = TradingEngine(Scripted({}), KiteBroker(GrowwAsKite(g), sleep=lambda s: None), risk=RISK)
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("SBIN", t, -1, 102.0, 95.0, False, False)
    engine.on_open("SBIN", t + pd.Timedelta(minutes=5), 100.0)
    assert engine.positions["SBIN"].side == -1
    g.move("SBIN", 95.0)
    engine.on_tick("SBIN", t + pd.Timedelta(minutes=9), 95.0)
    assert (engine.trades[0]["reason"], engine.trades[0]["exit_price"]) == ("target", 95.0)
    assert [o["transaction_type"] for o in g.placed] == ["SELL", "BUY", "BUY"]


def test_prices_and_candles_through_the_feed():
    data = {"SBIN": pd.concat([make_day("2026-01-02", [100.0] * 75), make_day("2026-01-05", [101.0] * 75)])}
    g = FakeGroww(data)
    g.price["SBIN"] = 101.5
    feed = KiteFeed(GrowwAsKite(g, sleep=lambda s: None), ["SBIN"], sleep=lambda s: None)
    assert feed.tick_sizes == {"SBIN": 0.05}
    assert feed.prices(["SBIN"]) == {"SBIN": 101.5}
    now = datetime(2026, 1, 5, 10, 2)
    candles = feed.candles("SBIN", datetime(2026, 1, 5, 9, 0), now)
    assert candles[0][0] == pd.Timestamp("2026-01-05 09:15") and candles[0][4] == 101.0
    assert candles[-1][0] == pd.Timestamp("2026-01-05 09:55")          # 10:00 candle still forming
    with pytest.raises(RuntimeError, match="BSEONLY"):
        KiteFeed(GrowwAsKite(g), ["BSEONLY"])


def test_history_download_asks_groww_in_15_day_pieces(tmp_path):
    market = make_synthetic(["AAA"], days=40, seed=2, end="2026-10-30")
    g = FakeGroww(market)
    saved = download(GrowwAsKite(g, sleep=lambda s: None), ["AAA"], tmp_path, days=60,
                     end=datetime(2026, 10, 30, 16, 0), sleep=lambda s: None, say=lambda m: None)
    assert saved == ["AAA"]
    spans = [(pd.Timestamp(e) - pd.Timestamp(s)).days for _, s, e, _ in g.candle_calls]
    assert len(spans) >= 4 and max(spans) <= 15 and {c[3] for c in g.candle_calls} == {5}
    pd.testing.assert_frame_equal(load_folder(tmp_path)["AAA"], market["AAA"], check_names=False, check_freq=False)


def test_connect_explains_what_is_missing():
    if importlib.util.find_spec("growwapi") is None:
        with pytest.raises(RuntimeError, match="pip install growwapi"):
            connect({})
        return
    try:
        import growwapi  # noqa: F401
    except Exception:
        pytest.skip("growwapi is present but its own dependencies are not")
    with pytest.raises(RuntimeError, match="GROWW_API_KEY"):
        connect({})
    with pytest.raises(RuntimeError, match="GROWW_TOTP_SECRET or GROWW_API_SECRET"):
        connect({"GROWW_API_KEY": "k"})
