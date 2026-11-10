# tradebot

Intraday equity trading bot for Indian stocks (Zerodha). It has two halves:

- **Research**: tests strategies on historical candles with real costs, so you know whether a strategy deserves real money before it gets any.
- **The bot**: runs one strategy through the day, on paper or for real, with a dashboard, a local database and Telegram alerts.

Real orders are only possible with `--live`, `LIVE_TRADING=yes` in `.env` and a typed confirmation. Everything else is simulation.

No strategy here has been tested on real market data yet, so nothing is known about profitability. Do the steps in order: backtest, then paper, then small real money.

## Setup

Python 3.10 or newer.

```bash
pip install -r requirements.txt
python -m pytest -q          # 84 tests, should all pass
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

## Run the bot

One command starts the bot and opens its dashboard in your browser. Stop it with Ctrl+C.

```bash
# 1. Demo on saved candles. No broker account needed.
python scripts/run_bot.py --strategy orb --replay data/sample

# 2. Paper trading: live Zerodha prices, simulated orders. Nothing leaves the laptop.
python scripts/kite_login.py
python scripts/run_bot.py --strategy orb

# 3. Real money. Read "Before going live" first.
python scripts/run_bot.py --strategy orb --live
```

Use the strategy and settings that passed walk-forward on real data, for example `--strategy orb --param or_minutes=30 --param rr=3`. Other options: `--symbols`, `--capital`, `--risk-pct`, `--no-short`, `--port`.

**How it trades.** A few seconds after each 5-minute candle closes, the bot fetches it, asks the strategy for a signal, passes it through the risk manager and enters at the current price. Between candles it checks the price of open positions every 3 seconds for the target, and squares everything off at 15:15. Paper mode fills orders with the backtester's rules, and a replay reproduces the backtest trade for trade (`tests/test_bot_engine.py`).

**Dashboard.** Shows today's result, open positions, closed trades, recent days and the log. **Pause** stops new entries and leaves open positions to their stops and targets. **Close everything** exits all positions and pauses. It listens on `127.0.0.1` only, so only this laptop can open it.

**Daily loss limit.** The bot counts open positions too. When closed plus open losses reach the limit, it closes everything and stops for the day.

**Database.** Trades, open positions and the log are kept in `data/tradebot.db` (SQLite). If the bot is restarted the same day it picks up its open positions and the day's counts.

**Telegram alerts.** Optional. Message @BotFather on Telegram, send `/newbot`, and copy the token. Send your new bot any message, open `https://api.telegram.org/bot<token>/getUpdates` and copy the `chat.id` number. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. You get a message on every entry and exit, on any pause or error, and a summary at the end of the day.

### Before going live

- Paper trade the strategy for at least two weeks and check it behaves like its backtest.
- Zerodha needs a whitelisted static IP for API orders. Set it in your Kite Connect app.
- Add `LIVE_TRADING=yes` to `.env`. The bot then asks you to type a phrase at every start.
- Start with small capital: `--capital 20000`.

What live mode does:

- **Entry**: a limit order 0.1% through the market. Whatever has not filled in 10 seconds is cancelled.
- **Stop-loss**: an SL-M order placed at the exchange the moment the entry fills. It stays active if the laptop sleeps or loses internet. If the stop order is refused, the position is closed immediately.
- **Target, square-off, signal exits**: the bot cancels the stop order and sends a market order. If the bot is not running, only the stop-loss protects the position, and Zerodha squares off intraday positions itself near 15:20.
- **If an order misbehaves**, or Zerodha's positions do not match the bot's records at start-up, the bot pauses and alerts you. It does not guess.

The order code is tested against a simulated broker (`tests/fake_kite.py`) that mirrors the `kiteconnect` library's calls. It has **not** been run against Zerodha's real servers. Watch the first live session closely, with the Kite app open, and trade the smallest size you can.

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

- In the backtester the daily loss limit counts closed trades only and does not force-close open positions. The bot does both, so on a very bad day the bot loses a little less than the backtest shows.
- Live and paper entries happen a few seconds after the candle closes, not at the exact next open the backtester assumes.
- Paper mode checks prices every 3 seconds, so a stop or target touched for an instant between checks can be missed or filled a little late.
- Profit and charges shown in live mode are the bot's own calculation. Zerodha's contract note is the final word.
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
  bot/
    engine.py      strategy -> risk manager -> broker, same order of decisions as the backtester
    broker.py      PaperBroker (simulated fills) and KiteBroker (real orders)
    kite.py        Zerodha session and price polling
    runner.py      replay loop and the live loop
    store.py       SQLite database
    dashboard.py   local web dashboard (dashboard.html is the page)
    alerts.py      Telegram
scripts/           command-line tools (run_bot.py starts the bot)
tests/             pytest suite
```

## Adding a strategy

Subclass `Strategy` in `tradebot/strategies/`, implement `signals(df)`, set `param_grid`, and add the class to `REGISTRY` in `strategies/__init__.py`. The no-lookahead test then covers it automatically.

## Roadmap

1. **Research layer**: backtester, five strategies, walk-forward validation. Built.
2. **Paper trading**: live Kite prices, simulated orders, same strategy and risk code. Built.
3. **Live trading**: real orders with exchange-side stop losses, behind an explicit switch. Built, not yet run against Zerodha.
4. **Dashboard and Telegram alerts**. Built.
5. **Still to do**: a walk-forward run on real Zerodha candles, which decides which strategy (if any) gets paper traded.
6. **Later**: strategy scoreboard, market-regime switching, F&O.
