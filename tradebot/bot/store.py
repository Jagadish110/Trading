"""Local SQLite database: trades, open positions and the event log.

Open positions are written the moment they are opened, so if the bot is
stopped or crashes mid-trade it can pick them up again on restart.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT PRIMARY KEY,
    mode        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    side        INTEGER NOT NULL,
    qty         INTEGER NOT NULL,
    entry_time  TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop        REAL NOT NULL,
    target      REAL NOT NULL,
    stop_order_id TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        INTEGER NOT NULL,
    qty         INTEGER NOT NULL,
    entry_time  TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_time   TEXT NOT NULL,
    exit_price  REAL NOT NULL,
    stop        REAL NOT NULL,
    target      REAL NOT NULL,
    reason      TEXT NOT NULL,
    gross       REAL NOT NULL,
    charges     REAL NOT NULL,
    net         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_exit ON trades (mode, exit_time);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def _iso(ts) -> str:
    return ts.isoformat(sep=" ", timespec="seconds") if hasattr(ts, "isoformat") else str(ts)


class Store:
    """All access goes through one connection guarded by a lock, because the
    dashboard reads from another thread."""

    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _run(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock, self._db:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # -- positions -------------------------------------------------------
    def save_position(self, mode: str, strategy: str, pos) -> None:
        self._run(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pos.symbol, mode, strategy, pos.side, pos.qty, _iso(pos.entry_time),
             pos.entry_price, pos.stop, pos.target, pos.stop_order_id),
        )

    def delete_position(self, symbol: str) -> None:
        self._run("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def open_positions(self, mode: str) -> list[dict]:
        return self._run("SELECT * FROM positions WHERE mode = ? ORDER BY entry_time", (mode,))

    # -- trades ----------------------------------------------------------
    def add_trade(self, mode: str, strategy: str, t: dict) -> None:
        self._run(
            "INSERT INTO trades (mode, strategy, symbol, side, qty, entry_time, entry_price,"
            " exit_time, exit_price, stop, target, reason, gross, charges, net)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mode, strategy, t["symbol"], t["side"], t["qty"], _iso(t["entry_time"]),
             t["entry_price"], _iso(t["exit_time"]), t["exit_price"], t["stop"], t["target"],
             t["reason"], t["gross"], t["charges"], t["net"]),
        )

    def trades(self, mode: str, day: str | None = None, limit: int = 500) -> list[dict]:
        """Newest first. `day` is 'YYYY-MM-DD' and filters on the exit date."""
        if day:
            return self._run(
                "SELECT * FROM trades WHERE mode = ? AND substr(exit_time, 1, 10) = ?"
                " ORDER BY id DESC LIMIT ?", (mode, day, limit))
        return self._run("SELECT * FROM trades WHERE mode = ? ORDER BY id DESC LIMIT ?", (mode, limit))

    def totals(self, mode: str) -> dict:
        row = self._run(
            "SELECT COUNT(*) AS trades, COALESCE(SUM(net), 0) AS net,"
            " COALESCE(SUM(net > 0), 0) AS wins, COALESCE(SUM(charges), 0) AS charges"
            " FROM trades WHERE mode = ?", (mode,))[0]
        return row

    def daily(self, mode: str) -> list[dict]:
        return self._run(
            "SELECT substr(exit_time, 1, 10) AS day, COUNT(*) AS trades, SUM(net) AS net"
            " FROM trades WHERE mode = ? GROUP BY day ORDER BY day", (mode,))

    # -- event log -------------------------------------------------------
    def log(self, level: str, message: str, ts=None) -> None:
        self._run("INSERT INTO events (ts, level, message) VALUES (?,?,?)",
                  (_iso(ts or datetime.now()), level, message))

    def events(self, limit: int = 100) -> list[dict]:
        return self._run("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
