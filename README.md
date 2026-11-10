# tradebot

Intraday equity trading bot for Indian stocks (Zerodha). This is **phase 1: the research layer**. It tests strategies on historical candles with real costs, so you know whether a strategy deserves real money before it gets any.

Nothing in this phase can place an order.

## Setup

Python 3.10 or newer.

```bash
pip install -r requirements.txt
python -m pytest -q          # 42 tests, should all pass
```

## Try it in two minutes (no broker account needed)

```bash
python scripts/make_sample_data.py
python scripts/run_backtest.py --data data/sample
python scripts/run_backtest.py --data data/sample --walk-forward
```

The sample data is **random fake candles**. Every strategy loses on it, and that is the correct result: there is no pattern in random data, so each trade just pays brokerage, taxes and slippage (roughly Rs 100 on a Rs 50,000 position). It proves the pipeline runs and shows how much costs matter. It says nothing about the real market.

## Test on real data

1. Create an app at developers.kite.trade. Market data needs the paid Connect plan.
2. Copy `.env.example` to `.env` and put your API key and secret in it. Never share or commit this file.
3. Log in and download history:

```bash
python scripts/kite_login.py                 # once per trading day
python scripts/fetch_history.py --days 750   # about 2 years of 5-minute candles
python scripts/run_backtest.py --data data/nse --walk-forward
```

The watchlist is `DEFAULT_WATCHLIST` in `tradebot/config.py`. Pass `--symbols` to override it.

The two Kite scripts follow the `kiteconnect` library's documented calls but could not be run against the live API while building this, so expect to fix a small thing or two on first use.

## Reading the results

| Column | Meaning |
|---|---|
| trades | number of round trips |
| win | share of trades that made money after costs |
| PF | profit factor: rupees won / rupees lost. Below 1 loses money |
| net Rs | profit after all costs |
| maxDD | worst fall from a peak, as % of the peak |
| sharpe | return relative to day-to-day swings, annualised |
| costs | brokerage, taxes and exchange charges paid (slippage is extra, inside the fills) |

`--walk-forward` prints two tables. **Believe the first one.** It picks settings on a 120-day window and scores them on the next 40 days, which the settings have never seen, then rolls forward. The second table shows the best settings picked with hindsight. A strategy that looks great in the second table and poor in the first was curve-fitted.

Verdicts on the first table:

- **PASS**: profitable on unseen data, profit factor at least 1.15, drawdown under 15%, at least 100 trades. This earns a strategy a paper-trading trial, nothing more.
- **FAIL**: do not trade it.

## How the backtester stays honest

- A signal is decided at a candle's close and filled at the next candle's open.
- If one candle touches both stop and target, the stop is assumed to hit first.
- A gap through the stop fills at the worse open price.
- Entries, stop exits and square-offs pay 5 bps of slippage. Every trade pays Zerodha's intraday charges (rates in `tradebot/config.py`, checked on 2026-11-10; re-check them now and then).
- New entries stop at 14:45. Everything is closed at 15:15.
- Each strategy is tested to confirm it cannot see future candles (`tests/test_strategies.py`).

## Risk rules (edit in `tradebot/config.py`)

| Setting | Default | Effect |
|---|---|---|
| `capital` | 100,000 | rupees positions are sized from |
| `risk_per_trade_pct` | 0.5 | loss if a stop is hit, as % of capital |
| `max_daily_loss_pct` | 2.0 | no new entries after losing this much in a day |
| `max_trades_per_day` | 6 | |
| `max_open_positions` | 3 | |
| `max_position_pct` | 50 | cap on one position's value. No leverage by default |

## Known limits

- The daily loss limit counts closed trades only. Open positions are protected by their own stops but are not force-closed when the limit is hit.
- Fills are modelled, not real. Illiquid stocks and fast markets fill worse than 5 bps.
- Testing today's index members on past data flatters results a little (stocks that dropped out are missing).
- Walk-forward assumes fixed position sizing (`compound=False`).

## Layout

```
tradebot/
  config.py        risk limits, session times, cost rates, watchlist
  data.py          load candle CSVs, generate sample data
  indicators.py    EMA, RSI, ATR, session VWAP, Supertrend
  costs.py         charges for one round trip
  risk.py          RiskManager: position size and the entry gate
  backtest.py      the simulation engine
  metrics.py       results summary
  walkforward.py   unseen-data validation and the verdict
  strategies/      orb, vwap_pullback, ema_trend, rsi_reversion, supertrend
scripts/           command-line tools
tests/             pytest suite
```

## Adding a strategy

Subclass `Strategy` in `tradebot/strategies/`, implement `signals(df)`, set `param_grid`, and add the class to `REGISTRY` in `strategies/__init__.py`. The no-lookahead test then covers it automatically.

## Roadmap

1. **Research layer** (this): backtester, five strategies, walk-forward validation.
2. **Paper trading**: live Kite prices, simulated orders, same strategy and risk code.
3. **Live trading**: real orders with exchange-side stop losses, behind an explicit switch. Needs a static IP.
4. **Desktop app**: dashboard, start/stop, Telegram alerts.
5. **Later**: strategy scoreboard, market-regime switching, F&O.
