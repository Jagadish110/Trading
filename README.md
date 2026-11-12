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
python -m pytest -q          # 122 tests, should all pass
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

## Telegram assistant

Message your bot on Telegram and it shows a menu. Pick one and it tells you when to buy and sell. It never places an order; you do that in your own app.

```bash
python scripts/run_telegram.py
```

| Menu option | What happens |
|---|---|
| **Swing trading** | Scans about 47 large stocks on daily candles now, then every weekday at 16:15. Sends what to buy and sell next morning, with the stop-loss and target to set as a GTT. Free prices from Yahoo Finance; no Zerodha API needed. |
| **Intraday equity** | Starts the assist mode described below. Needs the paid Kite Connect plan and the morning `kite_login.py`. Without them it replies with the steps to set it up. |
| **Options buying** | Checks one index lot against your capital and risk limit. Under about Rs 3 lakh it replies "blocked" with the numbers. Above that it says the strategy is not built yet. |
| **Futures & options selling** | Checks the exchange margin. Under about Rs 3.5 lakh it replies "not possible" with the numbers. Above that it says the strategy is not built yet. |

Also: **Status**, **Stop alerts**, and `/skip SYMBOL` if you did not take a swing trade.

Setup: create the bot and chat id as in "Telegram alerts" below, put them in `.env`, and set `TRADE_CAPITAL` to the rupees you trade with (default 20000). Only your chat id is answered. Keep the script running: on the laptop, or on any small always-on server (copy the folder, `pip install -r requirements.txt`, run the same command).

**How swing mode decides.** Three long-only strategies (`breakout`, `pullback`, `oversold_bounce`) are walk-forward tested once a week on three years of daily candles, with delivery charges. One is used only if it made money on unseen data; otherwise the bot sends no buys. Defaults: 1% of capital at risk per trade, at most 3 positions, each at most 30% of capital, sold after 15 trading days if neither stop nor target is hit. The bot assumes you take each buy at the next open.

Try it without Telegram or internet: `python scripts/swing_scan.py --demo`.

Not yet run against the real Telegram or Yahoo servers; both were tested against stand-ins. Yahoo's price address is unofficial and can stop working. The F&O figures (lot size 65, premium near Rs 150, margin near Rs 1.75 lakh) are rough and change; edit them in `tradebot/bot/telegram_bot.py`.

## Run the bot

One command starts the bot and opens its dashboard in your browser. Stop it with Ctrl+C.

```bash
# 1. Demo on saved candles. No broker account needed.
python scripts/run_bot.py --strategy orb --replay data/sample

# 2. Assist: the bot tells you when to buy and sell, you place the orders in your own app.
python scripts/kite_login.py
python scripts/run_bot.py --assist

# 3. Paper trading: live Zerodha prices, simulated orders. Nothing leaves the laptop.
python scripts/run_bot.py

# 4. Real money. Read "Before going live" first.
python scripts/run_bot.py --live
```

**You do not pick the strategy.** Once a week the bot downloads two years of candles, puts all five strategies through the walk-forward test, and trades the one that made money on data it had never seen, with the settings that were best over the most recent months. The choice and the reasons are printed at start-up and saved in `data/selection.json`. Run `python scripts/choose_strategy.py --data data/nse` to see the test on its own.

**If no strategy passes, the bot does not trade.** That is the system working: it means none of the five rules has an edge on recent data, and trading anyway would only pay costs. On the fake sample data this is always the outcome. A pass is a reason to paper trade, not a promise of profit.

To override the choice, name a strategy: `--strategy orb --param or_minutes=30 --param rr=3`. Other options: `--choose-again`, `--symbols`, `--capital`, `--risk-pct`, `--no-short`, `--port`.

**Assist mode.** The bot never places an order. A "What to do" box at the top of the dashboard (and Telegram, if set up) gives each instruction in full: what to buy or sell, how many shares, the stop-loss order to place straight after, the target, and the rupees at risk. Later it tells you when to exit and why. You place the orders yourself in Kite, Groww or any other app. The bot assumes you took each suggestion; press **I skipped it** if you did not, or **I closed it** if you got out early. It still needs the Zerodha login for live prices. Add `--no-short` if you only want buy-then-sell trades. Preview it without an account: `python scripts/run_bot.py --strategy orb --assist --replay data/sample`.

**How it trades.** A few seconds after each 5-minute candle closes, the bot fetches it, asks the strategy for a signal, passes it through the risk manager and enters at the current price. Between candles it checks the price of open positions every 3 seconds for the target, and squares everything off at 15:15. Paper mode fills orders with the backtester's rules, and a replay reproduces the backtest trade for trade (`tests/test_bot_engine.py`).

**Dashboard.** Shows today's result, open positions, closed trades, recent days and the log. **Pause** stops new entries and leaves open positions to their stops and targets. **Close everything** exits all positions and pauses. It listens on `127.0.0.1` only, so only this laptop can open it.

**Daily loss limit.** The bot counts open positions too. When closed plus open losses reach the limit, it closes everything and stops for the day.

**Database.** Trades, open positions and the log are kept in `data/tradebot.db` (SQLite). If the bot is restarted the same day it picks up its open positions and the day's counts.

**Telegram alerts.** Optional. Message @BotFather on Telegram, send `/newbot`, and copy the token. Send your new bot any message, open `https://api.telegram.org/bot<token>/getUpdates` and copy the `chat.id` number. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. You get a message on every entry and exit, on any pause or error, and a summary at the end of the day.

### Before going live

- Paper trade for at least two weeks and check the bot behaves like its backtest.
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
  select.py        picks the strategy to trade, or none
  strategies/      orb, vwap_pullback, ema_trend, rsi_reversion, supertrend
  swing/
    book.py        swing rules, shared by the backtest and the daily scan
    strategies.py  breakout, pullback, oversold_bounce (daily candles, long only)
    service.py     the daily scan and its message
    data.py        daily prices from Yahoo Finance
    config.py      swing risk limits, delivery charges, stock list
  bot/
    telegram_bot.py  the Telegram menu
    engine.py      strategy -> risk manager -> broker, same order of decisions as the backtester
    broker.py      PaperBroker (simulated fills) and KiteBroker (real orders)
    kite.py        Zerodha session and price polling
    history.py     candle history download
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
5. **Automatic strategy choice**: weekly walk-forward test, trades the winner or nothing. Built.
6. **Assist mode and Telegram menu**: swing and intraday advice, you place the orders. Built.
7. **Still to do**: the first runs on real prices, which show whether any strategy passes.
8. **Later**: options and futures strategies (blocked below the capital they need), market-regime switching.
