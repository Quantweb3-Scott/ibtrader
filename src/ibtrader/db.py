from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS ohlcv (ticker TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, vol REAL, adj_close REAL, source TEXT NOT NULL DEFAULT 'IB', PRIMARY KEY(ticker, trade_date));
CREATE TABLE IF NOT EXISTS asset_meta (ticker TEXT PRIMARY KEY, listed_date TEXT, exchange TEXT, currency TEXT DEFAULT 'USD', active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_data (ticker TEXT NOT NULL, ts_utc TEXT NOT NULL, last REAL, bid REAL, ask REAL, cumulative_volume REAL, source TEXT NOT NULL, PRIMARY KEY(ticker, ts_utc));
CREATE TABLE IF NOT EXISTS strategy_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_turnover_snapshot (trade_date TEXT NOT NULL, snapshot_ts_utc TEXT NOT NULL, snapshot_ts_ny TEXT NOT NULL, ticker TEXT NOT NULL, last_price REAL, cumulative_volume REAL, dollar_turnover REAL, source TEXT NOT NULL, PRIMARY KEY (trade_date, snapshot_ts_utc, ticker));
CREATE TABLE IF NOT EXISTS strategy_signal (trade_date TEXT PRIMARY KEY, selected_ticker TEXT, signal_ts_utc TEXT NOT NULL, signal_ts_ny TEXT NOT NULL, rank_json TEXT NOT NULL, is_tradeable INTEGER NOT NULL, skip_reason TEXT, final_turnover_selected_ticker TEXT, signal_matches_final INTEGER);
CREATE TABLE IF NOT EXISTS strategy_order (local_order_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, leg TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL, order_type TEXT NOT NULL, tif TEXT, quantity INTEGER NOT NULL, limit_price REAL, order_ref TEXT NOT NULL UNIQUE, ib_order_id INTEGER, ib_perm_id INTEGER, status TEXT NOT NULL, submitted_ts_utc TEXT, updated_ts_utc TEXT, reject_reason TEXT);
CREATE TABLE IF NOT EXISTS strategy_fill (id INTEGER PRIMARY KEY AUTOINCREMENT, local_order_id TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, commission REAL, fill_ts_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_nav (trade_date TEXT PRIMARY KEY, nav REAL NOT NULL, strategy_equity REAL NOT NULL, realized_pnl REAL, unrealized_pnl REAL, drawdown REAL, updated_ts_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_event (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, severity TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL, payload_json TEXT);
CREATE TABLE IF NOT EXISTS state_transition (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, reason TEXT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS account_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, net_liquidation REAL, settled_cash REAL, buying_power REAL);
CREATE TABLE IF NOT EXISTS position_snapshot (ts_utc TEXT NOT NULL, ticker TEXT NOT NULL, quantity REAL NOT NULL, average_cost REAL, PRIMARY KEY(ts_utc, ticker));
CREATE TABLE IF NOT EXISTS gateway_health (ts_utc TEXT PRIMARY KEY, connected INTEGER NOT NULL, latency_ms REAL, clock_skew_seconds REAL, message TEXT);
CREATE TABLE IF NOT EXISTS portfolio_snapshot (ts_utc TEXT PRIMARY KEY, net_liquidation REAL NOT NULL, settled_cash REAL NOT NULL, buying_power REAL NOT NULL, account_pnl REAL NOT NULL, normalized_nav REAL NOT NULL, strategy_market_value REAL NOT NULL, strategy_unrealized_pnl REAL NOT NULL, positions_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS performance_baseline (id INTEGER PRIMARY KEY CHECK(id=1), reset_ts_utc TEXT NOT NULL, net_liquidation REAL NOT NULL);
CREATE TABLE IF NOT EXISTS execution_ledger (execution_id TEXT PRIMARY KEY, order_ref TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, commission REAL NOT NULL, fill_ts_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_performance_baseline (id INTEGER PRIMARY KEY CHECK(id=1), reset_ts_utc TEXT NOT NULL, cumulative_pnl REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turnover_test_run (run_id TEXT PRIMARY KEY, started_ts_utc TEXT NOT NULL, ends_ts_utc TEXT NOT NULL, duration_seconds INTEGER NOT NULL, interval_seconds INTEGER NOT NULL, status TEXT NOT NULL, selected_ticker TEXT, rank_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS dry_run_position (id INTEGER PRIMARY KEY CHECK(id=1), ticker TEXT NOT NULL, quantity INTEGER NOT NULL, average_cost REAL NOT NULL, market_price REAL NOT NULL, opened_ts_utc TEXT NOT NULL, entry_order_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dry_run_fill (execution_id TEXT PRIMARY KEY, order_ref TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, commission REAL NOT NULL, fill_ts_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dry_run_performance (ts_utc TEXT PRIMARY KEY, nav REAL NOT NULL, normalized_nav REAL NOT NULL, pnl REAL NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL, commissions REAL NOT NULL);
CREATE TABLE IF NOT EXISTS variant_signal (trade_date TEXT NOT NULL, strategy_id TEXT NOT NULL, selected_tickers_json TEXT NOT NULL, signal_ts_utc TEXT NOT NULL, scores_json TEXT NOT NULL, is_tradeable INTEGER NOT NULL, skip_reason TEXT, PRIMARY KEY(trade_date, strategy_id));
CREATE TABLE IF NOT EXISTS variant_dry_run_position (strategy_id TEXT NOT NULL, ticker TEXT NOT NULL, quantity INTEGER NOT NULL, average_cost REAL NOT NULL, market_price REAL NOT NULL, opened_ts_utc TEXT NOT NULL, entry_order_ref TEXT NOT NULL, PRIMARY KEY(strategy_id, ticker));
CREATE TABLE IF NOT EXISTS variant_dry_run_fill (execution_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, order_ref TEXT NOT NULL, ticker TEXT NOT NULL, action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, commission REAL NOT NULL, fill_ts_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS variant_dry_run_performance (ts_utc TEXT NOT NULL, strategy_id TEXT NOT NULL, nav REAL NOT NULL, normalized_nav REAL NOT NULL, pnl REAL NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL, commissions REAL NOT NULL, PRIMARY KEY(ts_utc, strategy_id));
CREATE INDEX IF NOT EXISTS idx_risk_event_ts ON risk_event(ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_order_trade_date ON strategy_order(trade_date);
CREATE INDEX IF NOT EXISTS idx_portfolio_ts ON portfolio_snapshot(ts_utc DESC);
"""


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock, self.connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.lastrowid

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def event(
        self, severity: str, event_type: str, message: str, payload: dict | None = None
    ) -> None:
        self.execute(
            "INSERT INTO risk_event(ts_utc,severity,event_type,message,payload_json) VALUES(?,?,?,?,?)",
            (
                utc_now(),
                severity,
                event_type,
                message,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )

    def transition(
        self, from_state: str | None, to_state: str, reason: str, payload: dict | None = None
    ) -> None:
        self.execute(
            "INSERT INTO state_transition(ts_utc,from_state,to_state,reason,payload_json) VALUES(?,?,?,?,?)",
            (
                utc_now(),
                from_state,
                to_state,
                reason,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )

    def latest_state(self) -> str:
        rows = self.query("SELECT to_state FROM state_transition ORDER BY id DESC LIMIT 1")
        return rows[0]["to_state"] if rows else "IDLE"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
