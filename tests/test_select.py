"""The bot choosing its own strategy."""
from datetime import date

import pandas as pd
import pytest

from tradebot.config import RiskConfig
from tradebot.data import make_synthetic
from tradebot.select import Selection, choose
from tradebot.strategies import REGISTRY
from tradebot.strategies.base import Strategy


class Cheat(Strategy):
    """Looks six candles into the future. Only here to give the selector
    something that really does make money; never a real strategy."""
    name = "cheat"
    default_params = {"rr": 2.0}
    param_grid = {"rr": [1.5, 2.0]}

    def signals(self, df):
        out = self.empty(df)
        ahead = df["close"].shift(-6) / df["close"] - 1
        even = pd.Series(range(len(df)), index=df.index) % 12 == 0
        return self.fill(out, even & (ahead > 0.004), even & (ahead < -0.004), df["close"],
                         long_stop=df["close"] * 0.996, short_stop=df["close"] * 1.004, rr=self.params["rr"])


@pytest.fixture(scope="module")
def market():
    return make_synthetic(["AAA", "BBB", "CCC"], days=170, seed=5)


RISK = RiskConfig(max_trades_per_day=6)


def test_picks_the_strategy_that_passes_and_its_latest_settings(market):
    pick = choose(market, RISK, registry={"orb": REGISTRY["orb"], "cheat": Cheat},
                  train_days=100, test_days=30, today=date(2026, 11, 2))
    assert pick.strategy == "cheat"
    assert pick.params in ({"rr": 1.5}, {"rr": 2.0})
    verdicts = {r["strategy"]: r["verdict"] for r in pick.table}
    assert verdicts == {"orb": "FAIL", "cheat": "PASS"}
    assert "CHOSEN: cheat" in pick.report() and "not a promise" in pick.report()


def test_refuses_to_trade_when_nothing_passes(market):
    pick = choose(market, RISK, registry={"orb": REGISTRY["orb"], "rsi_reversion": REGISTRY["rsi_reversion"]},
                  train_days=100, test_days=30)
    assert pick.strategy is None and pick.params is None
    assert "Do not trade" in pick.report()


def test_choice_is_saved_reloaded_and_goes_stale(tmp_path):
    pick = Selection("2026-11-02", "2025-01-01", "2026-10-30", 20, "orb", {"rr": 2.0}, "why", [])
    pick.save(tmp_path / "x" / "selection.json")
    again = Selection.load(tmp_path / "x" / "selection.json")
    assert again == pick
    assert again.age_days(date(2026, 11, 5)) == 3 and again.age_days(date(2026, 11, 9)) == 7
    assert Selection.load(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert Selection.load(tmp_path / "bad.json") is None


def test_too_little_history_is_an_error_not_a_guess():
    small = make_synthetic(["AAA"], days=50, seed=1)
    with pytest.raises(ValueError, match="trading days"):
        choose(small, registry={"orb": REGISTRY["orb"]})


def test_history_download_writes_loadable_csvs(tmp_path):
    from datetime import datetime

    from fake_kite import FakeKite
    from tradebot.bot.history import download
    from tradebot.data import load_folder

    market = make_synthetic(["AAA", "BBB"], days=30, seed=2, end="2026-10-30")
    kite = FakeKite(market, now=lambda: datetime(2026, 10, 30, 16, 0))
    said = []
    saved = download(kite, ["AAA", "BBB", "NOPE"], tmp_path, days=200, end=datetime(2026, 10, 30, 16, 0),
                     sleep=lambda s: None, say=said.append)
    assert saved == ["AAA", "BBB"] and any("NOPE" in m and "skipped" in m for m in said)
    loaded = load_folder(tmp_path)
    pd.testing.assert_frame_equal(loaded["AAA"], market["AAA"], check_names=False, check_freq=False)
