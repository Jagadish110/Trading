"""Dashboard server and the start-up switch for real trading."""
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

from conftest import Scripted
from tradebot.bot.broker import PaperBroker
from tradebot.bot.dashboard import Dashboard
from tradebot.bot.engine import TradingEngine
from tradebot.config import CostConfig, RiskConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dash():
    costs = CostConfig(slippage_bps=0.0)
    engine = TradingEngine(Scripted({}), PaperBroker(costs), costs=costs,
                           risk=RiskConfig(risk_per_trade_pct=1.0, max_position_pct=100.0))
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("SBIN", t, 1, 98.0, 105.0, False, False)
    engine.on_open("SBIN", t + pd.Timedelta(minutes=5), 100.0)
    engine.on_tick("SBIN", t + pd.Timedelta(minutes=6), 101.0)
    d = Dashboard(engine, port=0).start()
    yield d
    d.stop()


def call(dash, path, method="GET", token=True, host=None):
    headers = {"X-Token": dash.token} if token else {}
    if host:
        headers["Host"] = host
    req = urllib.request.Request(dash.url.rstrip("/") + path, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_page_and_state(dash):
    status, body = call(dash, "/", token=False)
    assert status == 200 and dash.token.encode() in body and b"__TOKEN__" not in body
    status, body = call(dash, "/api/state")
    state = json.loads(body)
    assert status == 200 and state["mode"] == "paper" and state["status"] == "running"
    assert state["positions"][0]["symbol"] == "SBIN"
    assert state["unrealised"] == pytest.approx(500.0)       # 500 shares, up Rs 1


def test_requests_without_the_token_or_from_another_site_are_refused(dash):
    assert call(dash, "/api/state", token=False)[0] == 403
    assert call(dash, "/api/kill", "POST", token=False)[0] == 403
    assert call(dash, "/api/state", host="evil.example.com")[0] == 403
    assert "SBIN" in dash.engine.positions


def test_pause_resume_and_kill_buttons(dash):
    assert json.loads(call(dash, "/api/pause", "POST")[1])["status"] == "paused"
    assert dash.engine.risk.halted
    assert json.loads(call(dash, "/api/resume", "POST")[1])["status"] == "running"
    state = json.loads(call(dash, "/api/kill", "POST")[1])
    assert state["positions"] == [] and state["status"] == "paused"
    assert state["trades"][0]["reason"] == "manual"
    assert call(dash, "/api/nope", "POST")[0] == 404


def test_dashboard_only_listens_on_this_computer(dash):
    assert dash.server.server_address[0] == "127.0.0.1"


def run_bot(*args, stdin="", env_extra=None):
    env = {"PATH": "/usr/bin:/bin", "KITE_API_KEY": "", **(env_extra or {})}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "run_bot.py"), *args],
                          input=stdin, capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.skipif((ROOT / ".env").exists(), reason="a real .env is present; not touching live settings")
def test_live_mode_needs_the_env_switch_and_the_typed_phrase():
    out = run_bot("--strategy", "orb", "--live")
    assert out.returncode != 0 and "LIVE_TRADING=yes" in out.stderr
    out = run_bot("--live", env_extra={"LIVE_TRADING": "yes"})       # switch on, but no Zerodha login
    assert out.returncode != 0 and "KITE_API_KEY" in out.stderr
    out = run_bot("--strategy", "orb", "--live", "--replay", "x")
    assert out.returncode != 0 and "cannot be combined" in out.stderr


def test_unknown_strategy_setting_is_refused():
    out = run_bot("--strategy", "orb", "--param", "nonsense=1", "--replay", "x")
    assert out.returncode != 0 and "Unknown setting" in out.stderr


def test_live_start_needs_the_exact_phrase(capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import run_bot as launcher
    from tradebot.strategies import REGISTRY

    strategy, risk = REGISTRY["orb"](), RiskConfig(capital=20_000)
    assert not launcher.confirm_live(strategy, risk, ask=lambda prompt: "yes")
    assert not launcher.confirm_live(strategy, risk, ask=lambda prompt: "trade real money")
    assert launcher.confirm_live(strategy, risk, ask=lambda prompt: " TRADE REAL MONEY ")
    shown = capsys.readouterr().out
    assert "REAL MONEY" in shown and "Rs 20,000" in shown and "about Rs 100" in shown


def test_auto_is_the_default_and_refuses_to_trade_fake_data(tmp_path):
    from tradebot.data import make_synthetic, save_folder
    save_folder(make_synthetic(["AAA", "BBB"], days=200, seed=4), tmp_path)
    out = run_bot("--replay", str(tmp_path), "--no-browser")
    assert out.returncode != 0
    assert "CHOSEN: nothing" in out.stdout and "Nothing to trade" in out.stderr


# -- assist mode: the bot advises, the user places the orders ------------
@pytest.fixture
def assist():
    from tradebot.bot.alerts import TelegramAlerts
    from tradebot.bot.engine import EngineConfig
    costs = CostConfig(slippage_bps=0.0)
    sent = []
    alerts = TelegramAlerts("T", "1", post=lambda url, p: sent.append(p["text"]))
    engine = TradingEngine(Scripted({}), PaperBroker(costs), costs=costs, alerts=alerts, mode="assist",
                           cfg=EngineConfig(advise=True),
                           risk=RiskConfig(risk_per_trade_pct=1.0, max_position_pct=100.0))
    t = pd.Timestamp("2026-01-05 10:00")
    engine.set_intent("SBIN", t, 1, 98.0, 105.0, False, False)
    engine.on_open("SBIN", t + pd.Timedelta(minutes=5), 100.0)
    engine.set_intent("INFY", t, -1, 510.0, 480.0, False, False)
    engine.on_open("INFY", t + pd.Timedelta(minutes=5), 500.0)
    return engine, alerts, sent, t


def test_assist_gives_complete_buy_and_sell_instructions(assist):
    engine, alerts, sent, t = assist
    buy, short = [a["text"] for a in engine.advice]
    assert buy == ("BUY 500 SBIN now as an intraday order, near 100.00. Straight after, place a stop-loss "
                   "SELL order at 98.00. Take profit at 105.00. If the stop is hit you lose about Rs 1,000.")
    assert short.startswith("SELL SHORT 100 INFY now") and "stop-loss BUY order at 510.00" in short

    engine.on_tick("SBIN", t + pd.Timedelta(minutes=8), 105.0)
    engine.on_tick("INFY", t + pd.Timedelta(minutes=9), 511.0)
    alerts.close()
    sell, stopped = [a["text"] for a in engine.advice][2:]
    assert sell.startswith("SELL 500 SBIN now, near 105.00: target reached. Then cancel its stop-loss order.")
    assert stopped.startswith("INFY: price hit the stop at 510.00.") and "check that it did" in stopped
    assert sent == [a["text"] for a in engine.advice]            # the same words reach the phone
    assert engine.snapshot()["advice"][0]["text"] == stopped     # newest first on the dashboard


def test_assist_skip_forgets_the_trade_and_close_records_it(assist):
    engine, alerts, _, t = assist
    alerts.close()
    assert engine.risk.trades_today == 2
    d = Dashboard(engine, port=0).start()
    try:
        state = json.loads(call(d, "/api/skip?symbol=SBIN", "POST")[1])
        assert [p["symbol"] for p in state["positions"]] == ["INFY"] and state["trades"] == []
        assert engine.risk.trades_today == 1 and engine.store.open_positions("assist")[0]["symbol"] == "INFY"
        engine.on_tick("INFY", t + pd.Timedelta(minutes=7), 495.0)
        state = json.loads(call(d, "/api/close?symbol=INFY", "POST")[1])
        assert state["positions"] == [] and state["mode"] == "assist" and state["advise"] is True
        assert (state["trades"][0]["reason"], state["trades"][0]["exit_price"]) == ("manual", 495.0)
        assert call(d, "/api/close", "POST")[0] == 404
        assert call(d, "/api/skip?symbol=SBIN", "POST", token=False)[0] == 403
    finally:
        d.stop()


def test_assist_cannot_be_combined_with_live():
    out = run_bot("--assist", "--live")
    assert out.returncode != 0 and "cannot be combined" in out.stderr
