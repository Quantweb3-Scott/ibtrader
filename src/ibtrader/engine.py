from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .alerts import AlertManager
from .broker import BrokerAdapter
from .config import Settings
from .db import Database, utc_now
from .models import BrokerOrder, OrderRequest, Position, Quote, TradingState
from .risk import RiskManager

logger = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
FINAL_ORDER_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
AGGRESSIVE = "mag7_overnight_z120_top1_qqq_sma200"
ROBUST = "mag7_overnight_z120_top3_intraday_up_qqq_sma100"


class TradingEngine:
    def __init__(
        self, settings: Settings, db: Database, broker: BrokerAdapter, alerts: AlertManager
    ):
        self.settings, self.db, self.broker, self.alerts = settings, db, broker, alerts
        self.risk = RiskManager(settings)
        self.state = TradingState(db.latest_state())
        self.last_quotes: dict[str, Quote] = {}
        self.account_synced = False
        self.position_cache: list[Position] = []
        self.all_position_cache: list[Position] = []
        self.latest_portfolio: dict = {}
        self.latest_dry_run: dict = {}
        self._portfolio_stopped = asyncio.Event()
        self._lock = asyncio.Lock()

    def transition(self, state: TradingState, reason: str, payload: dict | None = None) -> None:
        previous = self.state
        self.db.transition(previous.value, state.value, reason, payload)
        self.state = state

    async def reconcile(self) -> None:
        async with self._lock:
            positions = await self.broker.positions()
            strategy_positions = self.strategy_positions(positions)
            orders = await self.broker.open_orders()
            self.all_position_cache = positions
            self.position_cache = strategy_positions
            db_orders = self.db.query(
                "SELECT * FROM strategy_order WHERE status NOT IN ('Filled','Cancelled','ApiCancelled','Inactive')"
            )
            db_refs = {row["order_ref"] for row in db_orders}
            unknown_orders = [
                o.request.order_ref
                for o in orders
                if o.request.order_ref.startswith("turnover_top1:")
                and o.request.order_ref not in db_refs
            ]
            db_position = self.db.query(
                "SELECT ticker, quantity FROM position_snapshot ORDER BY ts_utc DESC LIMIT 1"
            )
            if unknown_orders:
                self.transition(
                    TradingState.HALTED, "unknown IB strategy order", {"refs": unknown_orders}
                )
                await self.alerts.send(
                    "order_reconciliation_failed",
                    "CRITICAL",
                    "IB中存在数据库未知的策略订单",
                    {"refs": unknown_orders},
                )
                return
            if (
                strategy_positions
                and not db_position
                and self.state not in {TradingState.HOLD_OVERNIGHT, TradingState.SELL_SUBMITTED}
            ):
                self.transition(
                    TradingState.HALTED,
                    "unexpected position",
                    {"positions": [asdict(p) for p in strategy_positions]},
                )
                await self.alerts.send(
                    "unexpected_position", "CRITICAL", "IB持仓与策略状态不一致，已停机"
                )
                return
            self.account_synced = True

    def strategy_positions(self, positions: list[Position]) -> list[Position]:
        universe = set(self.settings.strategy.universe)
        return [position for position in positions if position.ticker in universe]

    async def portfolio_loop(self) -> None:
        while not self._portfolio_stopped.is_set():
            if self.broker.is_connected():
                try:
                    await self.snapshot_portfolio()
                    self.latest_dry_run = await self.snapshot_dry_run_performance()
                except Exception as exc:  # noqa: BLE001 - monitoring must recover next cycle
                    logger.warning("portfolio snapshot failed: %s", exc)
            await asyncio.sleep(60)

    def stop_portfolio_loop(self) -> None:
        self._portfolio_stopped.set()

    async def snapshot_portfolio(self) -> dict:
        await self.sync_execution_fills()
        account = await self.broker.account()
        positions = await self.broker.positions()
        self.all_position_cache = positions
        self.position_cache = self.strategy_positions(positions)
        baseline_rows = self.db.query("SELECT * FROM performance_baseline WHERE id=1")
        if not baseline_rows:
            self.db.execute(
                "INSERT INTO performance_baseline(id,reset_ts_utc,net_liquidation) VALUES(1,?,?)",
                (account.timestamp.isoformat(), account.net_liquidation),
            )
            baseline_nav = account.net_liquidation
            reset_at = account.timestamp.isoformat()
        else:
            baseline_nav = float(baseline_rows[0]["net_liquidation"])
            reset_at = baseline_rows[0]["reset_ts_utc"]
        account_pnl = account.net_liquidation - baseline_nav
        normalized_nav = account.net_liquidation / baseline_nav if baseline_nav else 1.0
        strategy_market_value = sum(float(p.market_value or 0) for p in self.position_cache)
        strategy_unrealized_pnl = sum(float(p.unrealized_pnl or 0) for p in self.position_cache)
        strategy_pnl_total, strategy_commission = self.strategy_pnl(strategy_market_value)
        strategy_baseline = self.db.query("SELECT * FROM strategy_performance_baseline WHERE id=1")
        strategy_base_pnl = (
            float(strategy_baseline[0]["cumulative_pnl"]) if strategy_baseline else 0.0
        )
        position_payload = [
            {
                **asdict(position),
                "is_strategy": position.ticker in self.settings.strategy.universe,
            }
            for position in positions
        ]
        snapshot = {
            "timestamp": account.timestamp.isoformat(),
            "net_liquidation": account.net_liquidation,
            "settled_cash": account.settled_cash,
            "buying_power": account.buying_power,
            "account_pnl": account_pnl,
            "normalized_nav": normalized_nav,
            "baseline_reset_at": reset_at,
            "strategy_market_value": strategy_market_value,
            "strategy_unrealized_pnl": strategy_unrealized_pnl,
            "strategy_pnl": strategy_pnl_total - strategy_base_pnl,
            "strategy_commission": strategy_commission,
            "positions": position_payload,
        }
        self.db.execute(
            "INSERT OR REPLACE INTO portfolio_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot["timestamp"],
                account.net_liquidation,
                account.settled_cash,
                account.buying_power,
                account_pnl,
                normalized_nav,
                strategy_market_value,
                strategy_unrealized_pnl,
                json.dumps(position_payload),
            ),
        )
        self.latest_portfolio = snapshot
        return snapshot

    async def start_turnover_test(self) -> dict:
        """Take one on-demand turnover snapshot and return its ranking immediately."""
        if not self.broker.is_connected():
            raise RuntimeError("IB Gateway disconnected")
        run_id = str(uuid.uuid4())
        started = datetime.now(UTC)
        try:
            quotes, market_data_mode = await self.broker.turnover_test_quotes(
                self.settings.strategy.universe
            )
            day = datetime.now(UTC).astimezone(NY).date()
            for quote in quotes:
                self.last_quotes[quote.ticker] = quote
                self.db.execute(
                    "INSERT OR REPLACE INTO live_turnover_snapshot VALUES(?,?,?,?,?,?,?,?)",
                    (
                        str(day),
                        quote.timestamp.astimezone(UTC).isoformat(),
                        quote.timestamp.astimezone(NY).isoformat(),
                        quote.ticker,
                        quote.last,
                        quote.cumulative_volume,
                        quote.dollar_turnover,
                        f"IB_{market_data_mode}",
                    ),
                )
            rank = [
                {
                    "ticker": quote.ticker,
                    "last": quote.last,
                    "volume": quote.cumulative_volume,
                    "test_turnover": quote.last * quote.cumulative_volume,
                    "market_data_mode": market_data_mode,
                }
                for quote in quotes
                if quote.ticker in self.settings.strategy.universe
                and math.isfinite(quote.last)
                and math.isfinite(quote.cumulative_volume)
                and quote.last > 0
                and quote.cumulative_volume >= 0
            ]
            if not rank:
                raise RuntimeError("IB returned no valid real-time quotes; check market data")
            rank.sort(key=lambda item: item["test_turnover"], reverse=True)
            selected = rank[0]["ticker"] if rank else None
            self.db.execute(
                "INSERT INTO turnover_test_run(run_id,started_ts_utc,ends_ts_utc,duration_seconds,"
                "interval_seconds,status,selected_ticker,rank_json) VALUES(?,?,?,?,?,'COMPLETED',?,?)",
                (
                    run_id,
                    started.isoformat(),
                    datetime.now(UTC).isoformat(),
                    0,
                    0,
                    selected,
                    json.dumps(rank),
                ),
            )
            return {
                "run_id": run_id,
                "selected_ticker": selected,
                "market_data_mode": market_data_mode,
                "rank": rank,
            }
        except Exception as exc:
            self.db.execute(
                "INSERT INTO turnover_test_run(run_id,started_ts_utc,ends_ts_utc,duration_seconds,"
                "interval_seconds,status,error) VALUES(?,?,?,?,?,'FAILED',?)",
                (run_id, started.isoformat(), datetime.now(UTC).isoformat(), 0, 0, str(exc)),
            )
            raise RuntimeError(f"turnover snapshot failed: {exc}") from exc

    def dry_run_commission(self, quantity: float) -> float:
        return max(
            self.settings.execution.dry_run_min_commission_usd,
            abs(quantity) * self.settings.execution.dry_run_commission_per_share_usd,
        )

    @staticmethod
    def _opposing_price(quote: Quote, action: str) -> float | None:
        """Return the executable opposing quote: BUY crosses ask; SELL crosses bid."""
        value = quote.ask if action.upper() == "BUY" else quote.bid
        return float(value) if value is not None and math.isfinite(value) and value > 0 else None

    def dry_run_pnl(self, market_value: float) -> tuple[float, float, float]:
        rows = self.db.query("SELECT action,quantity,price,commission FROM dry_run_fill")
        commissions = sum(float(row["commission"]) for row in rows)
        cashflow = sum(
            float(row["quantity"])
            * float(row["price"])
            * (1 if str(row["action"]).upper() == "SELL" else -1)
            for row in rows
        )
        pnl = cashflow + market_value - commissions
        return pnl, cashflow, commissions

    async def snapshot_dry_run_performance(self) -> dict:
        variant_positions = self.db.query("SELECT * FROM variant_dry_run_position")
        if variant_positions or self.db.query("SELECT 1 FROM variant_dry_run_fill LIMIT 1"):
            return await self._snapshot_variant_dry_run_performance(variant_positions)
        positions = self.db.query("SELECT * FROM dry_run_position WHERE id=1")
        market_value = 0.0
        position = None
        if positions:
            position = positions[0]
            quotes = await self.broker.quotes([position["ticker"]])
            market_price = quotes[0].last if quotes else float(position["market_price"])
            market_value = market_price * int(position["quantity"])
            self.db.execute(
                "UPDATE dry_run_position SET market_price=? WHERE id=1", (market_price,)
            )
            position = {**position, "market_price": market_price, "market_value": market_value}
        pnl, cashflow, commissions = self.dry_run_pnl(market_value)
        capital = self.settings.risk.initial_strategy_capital_usd
        nav = capital + pnl
        snapshot = {
            "timestamp": utc_now(),
            "nav": nav,
            "normalized_nav": nav / capital if capital else 1.0,
            "pnl": pnl,
            "cash": capital + cashflow - commissions,
            "market_value": market_value,
            "commissions": commissions,
            "position": position,
        }
        self.db.execute(
            "INSERT OR REPLACE INTO dry_run_performance VALUES(?,?,?,?,?,?,?)",
            (
                snapshot["timestamp"],
                nav,
                snapshot["normalized_nav"],
                pnl,
                snapshot["cash"],
                market_value,
                commissions,
            ),
        )
        return snapshot

    async def _snapshot_variant_dry_run_performance(self, positions: list[dict]) -> dict:
        quotes = await self.broker.quotes(list({row["ticker"] for row in positions})) if positions else []
        prices = {quote.ticker: quote.last for quote in quotes}
        strategies = {}
        for strategy_id in (ROBUST, AGGRESSIVE):
            strategy_positions = [row for row in positions if row["strategy_id"] == strategy_id]
            market_value = 0.0
            rendered_positions = []
            for row in strategy_positions:
                price = prices.get(row["ticker"], float(row["market_price"]))
                value = price * int(row["quantity"])
                market_value += value
                self.db.execute(
                    "UPDATE variant_dry_run_position SET market_price=? WHERE strategy_id=? AND ticker=?",
                    (price, strategy_id, row["ticker"]),
                )
                rendered_positions.append({**row, "market_price": price, "market_value": value})
            fills = self.db.query(
                "SELECT action,quantity,price,commission FROM variant_dry_run_fill WHERE strategy_id=?",
                (strategy_id,),
            )
            commissions = sum(float(row["commission"]) for row in fills)
            cashflow = sum(float(row["quantity"]) * float(row["price"]) *
                           (1 if row["action"] == "SELL" else -1) for row in fills)
            capital = self.settings.risk.initial_strategy_capital_usd
            pnl = cashflow + market_value - commissions
            nav = capital + pnl
            snapshot_time = utc_now()
            self.db.execute(
                "INSERT OR REPLACE INTO variant_dry_run_performance VALUES(?,?,?,?,?,?,?,?)",
                (snapshot_time, strategy_id, nav, nav / capital if capital else 1.0,
                 pnl, capital + cashflow - commissions, market_value, commissions),
            )
            strategies[strategy_id] = {
                "strategy_id": strategy_id, "nav": nav,
                "normalized_nav": nav / capital if capital else 1.0, "pnl": pnl,
                "cash": capital + cashflow - commissions, "market_value": market_value,
                "commissions": commissions, "positions": rendered_positions,
            }
        total_capital = self.settings.risk.initial_strategy_capital_usd * len(strategies)
        total_nav = sum(item["nav"] for item in strategies.values())
        all_positions = [position for item in strategies.values() for position in item["positions"]]
        return {
            "timestamp": utc_now(), "nav": total_nav,
            "normalized_nav": total_nav / total_capital if total_capital else 1.0,
            "pnl": sum(item["pnl"] for item in strategies.values()),
            "cash": sum(item["cash"] for item in strategies.values()),
            "market_value": sum(item["market_value"] for item in strategies.values()),
            "commissions": sum(item["commissions"] for item in strategies.values()),
            "positions": all_positions, "position": all_positions[0] if len(all_positions) == 1 else None,
            "strategies": strategies,
        }

    async def reset_dry_run(self) -> None:
        self.db.execute("DELETE FROM dry_run_position")
        self.db.execute("DELETE FROM dry_run_fill")
        self.db.execute("DELETE FROM dry_run_performance")
        self.db.execute("DELETE FROM variant_dry_run_position")
        self.db.execute("DELETE FROM variant_dry_run_fill")
        self.db.execute("DELETE FROM variant_dry_run_performance")
        self.db.event("INFO", "dry_run_reset", "DRY_RUN 盈亏、净值和模拟仓位已重置")

    async def manual_real_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        order_type: str,
        limit_price: float | None = None,
    ) -> BrokerOrder | None:
        ticker = ticker.upper().strip()
        action = action.upper().strip()
        order_type = order_type.upper().strip()
        if not self.settings.risk.trading_enabled:
            raise RuntimeError("REAL order requires trading_enabled=true")
        if not self.settings.risk.manual_real_order_enabled:
            raise RuntimeError("REAL order requires manual_real_order_enabled=true")
        if self.settings.ib.readonly_mode:
            raise RuntimeError("REAL order requires ib.readonly_mode=false")
        if ticker not in self.settings.strategy.universe:
            raise RuntimeError("ticker is not in the strategy allowlist")
        if quantity < 1:
            raise RuntimeError("quantity must be positive")
        if action not in {"BUY", "SELL"}:
            raise RuntimeError("action must be BUY or SELL")
        if order_type not in {"LOC", "MOC"}:
            raise RuntimeError("order_type must be LOC or MOC")
        if not self.broker.is_connected():
            raise RuntimeError("IB Gateway disconnected")
        positions = self.strategy_positions(await self.broker.positions())
        ticker_position = next((p for p in positions if p.ticker == ticker), None)
        if action == "BUY" and positions:
            raise RuntimeError("strategy position already exists")
        if action == "SELL" and (ticker_position is None or ticker_position.quantity < quantity):
            raise RuntimeError("SELL quantity exceeds the current long position")
        active = [
            order
            for order in await self.broker.open_orders()
            if order.request.order_ref.startswith("turnover_top1:")
        ]
        if active:
            raise RuntimeError("an active strategy order already exists")
        quotes = await self.broker.quotes([ticker])
        if not quotes:
            raise RuntimeError("fresh quote unavailable")
        quote = quotes[0]
        if (datetime.now(UTC) - quote.timestamp.astimezone(UTC)).total_seconds() >= 5:
            raise RuntimeError("market data is stale")
        reference_price = quote.last
        if order_type == "LOC":
            slippage = self.settings.execution.max_entry_slippage_bps / 10_000
            raw_price = limit_price or (
                reference_price * (1 + slippage if action == "BUY" else 1 - slippage)
            )
            limit_price = (
                math.floor(raw_price * 100 + 1e-9) / 100
                if action == "BUY"
                else math.ceil(raw_price * 100 - 1e-9) / 100
            )
        notional = quantity * (limit_price or reference_price)
        account = await self.broker.account()
        available_cash = max(0.0, account.settled_cash - self.settings.risk.cash_reserve_usd)
        if action == "BUY" and notional > min(
            available_cash,
            self.settings.risk.initial_strategy_capital_usd,
            self.settings.risk.max_single_order_notional_usd,
            self.settings.risk.max_position_notional_usd,
        ):
            raise RuntimeError("manual order exceeds cash or configured notional limit")
        trade_day = datetime.now(NY).date()
        request = OrderRequest(
            ticker,
            action,
            quantity,
            order_type,
            "DAY",
            limit_price if order_type == "LOC" else None,
            f"turnover_top1:{trade_day}:manual_{action.lower()}:{ticker}:1",
        )
        return await self._submit(
            trade_day,
            "manual_entry" if action == "BUY" else "manual_exit",
            request,
            force_real=True,
        )

    async def sync_execution_fills(self) -> None:
        for fill in await self.broker.execution_fills():
            self.db.execute(
                "INSERT INTO execution_ledger VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(execution_id) DO UPDATE SET commission=excluded.commission,price=excluded.price,quantity=excluded.quantity,fill_ts_utc=excluded.fill_ts_utc",
                (
                    fill.execution_id,
                    fill.order_ref,
                    fill.ticker,
                    fill.action,
                    fill.quantity,
                    fill.price,
                    fill.commission,
                    fill.timestamp.isoformat(),
                ),
            )

    def strategy_pnl(self, market_value: float) -> tuple[float, float]:
        rows = self.db.query("SELECT action,quantity,price,commission FROM execution_ledger")
        commission = sum(float(row["commission"] or 0) for row in rows)
        cashflow = sum(
            float(row["quantity"])
            * float(row["price"])
            * (1 if str(row["action"]).upper() in {"SELL", "SLD"} else -1)
            for row in rows
        )
        return cashflow + market_value - commission, commission

    async def reset_performance(self) -> dict:
        snapshot = await self.snapshot_portfolio()
        now = utc_now()
        self.db.execute(
            "INSERT OR REPLACE INTO performance_baseline(id,reset_ts_utc,net_liquidation) VALUES(1,?,?)",
            (now, snapshot["net_liquidation"]),
        )
        cumulative_strategy_pnl, _ = self.strategy_pnl(snapshot["strategy_market_value"])
        self.db.execute(
            "INSERT OR REPLACE INTO strategy_performance_baseline(id,reset_ts_utc,cumulative_pnl) VALUES(1,?,?)",
            (now, cumulative_strategy_pnl),
        )
        self.db.execute("DELETE FROM portfolio_snapshot")
        snapshot["account_pnl"] = 0.0
        snapshot["normalized_nav"] = 1.0
        snapshot["baseline_reset_at"] = now
        snapshot["strategy_pnl"] = 0.0
        self.db.execute(
            "INSERT INTO portfolio_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot["timestamp"],
                snapshot["net_liquidation"],
                snapshot["settled_cash"],
                snapshot["buying_power"],
                0.0,
                1.0,
                snapshot["strategy_market_value"],
                snapshot["strategy_unrealized_pnl"],
                json.dumps(snapshot["positions"]),
            ),
        )
        self.latest_portfolio = snapshot
        self.db.event("INFO", "performance_reset", "净值与盈亏基准已重置")
        return snapshot

    async def snapshot_quotes(self, trade_date: date | None = None) -> list[Quote]:
        tickers = list(dict.fromkeys([
            *self.settings.strategy.universe,
            self.settings.strategy.benchmark_ticker,
        ]))
        quotes = await self.broker.quotes(tickers)
        now_ny = datetime.now(UTC).astimezone(NY)
        day = trade_date or now_ny.date()
        for quote in quotes:
            self.last_quotes[quote.ticker] = quote
            self.db.execute(
                "INSERT OR REPLACE INTO live_turnover_snapshot VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(day),
                    quote.timestamp.astimezone(UTC).isoformat(),
                    quote.timestamp.astimezone(NY).isoformat(),
                    quote.ticker,
                    quote.last,
                    quote.cumulative_volume,
                    quote.dollar_turnover,
                    "IB",
                ),
            )
        return quotes

    async def refresh_history(self) -> None:
        if not self.broker.is_connected():
            await self.alerts.send(
                "history_refresh_failed", "WARNING", "IB Gateway 离线，日线更新已跳过"
            )
            return
        tickers = list(dict.fromkeys([
            *self.settings.strategy.universe,
            self.settings.strategy.benchmark_ticker,
        ]))
        for ticker in tickers:
            try:
                bars = await self.broker.historical_daily_bars(ticker, 220)
                for bar in bars:
                    self.db.execute(
                        "INSERT OR REPLACE INTO ohlcv(ticker,trade_date,open,high,low,close,vol,source) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            ticker,
                            bar["trade_date"],
                            bar["open"],
                            bar["high"],
                            bar["low"],
                            bar["close"],
                            bar["vol"],
                            "IB",
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - continue other symbols, alert failure
                await self.alerts.send(
                    f"history_refresh_failed:{ticker}", "WARNING", f"{ticker} 日线更新失败: {exc}"
                )

    async def preflight_close(self) -> None:
        if self.state == TradingState.HALTED:
            return
        self.transition(TradingState.PREFLIGHT_CLOSE, "scheduled close preflight")
        health = await self.broker.health()
        if not health.connected:
            await self.skip_entry("gateway_disconnected")
            return
        await self.reconcile()
        await self.snapshot_quotes()

    def _rank(self, quotes: list[Quote]) -> list[Quote]:
        eligible = []
        for quote in quotes:
            if (
                quote.ticker in self.settings.strategy.universe
                and quote.last >= self.settings.strategy.min_price
                and not quote.halted
                and self._history_eligible(quote.ticker)
            ):
                eligible.append(quote)
        return sorted(eligible, key=lambda q: q.dollar_turnover, reverse=True)

    def _history_eligible(self, ticker: str) -> bool:
        rows = self.db.query(
            "SELECT close, vol FROM ohlcv WHERE ticker=? ORDER BY trade_date DESC LIMIT ?",
            (ticker, self.settings.strategy.min_listed_trading_days),
        )
        if len(rows) < self.settings.strategy.min_listed_trading_days:
            return False
        avg_dollar_volume = sum(
            float(row["close"] or 0) * float(row["vol"] or 0) for row in rows
        ) / len(rows)
        return avg_dollar_volume >= self.settings.strategy.min_avg_dollar_volume_20d

    def _turnover_zscore(self, quote: Quote) -> float | None:
        lookback = self.settings.strategy.turnover_zscore_lookback
        rows = self.db.query(
            "SELECT close,vol FROM ohlcv WHERE ticker=? ORDER BY trade_date DESC LIMIT ?",
            (quote.ticker, lookback - 1),
        )
        history = [float(row["close"] or 0) * float(row["vol"] or 0) for row in rows]
        if len(history) < lookback - 1:
            return None
        sample = [quote.dollar_turnover, *history]
        deviation = statistics.pstdev(sample)
        return (quote.dollar_turnover - statistics.fmean(sample)) / deviation if deviation else 0.0

    def _benchmark_above_sma(self, benchmark: Quote, window: int) -> bool | None:
        rows = self.db.query(
            "SELECT close FROM ohlcv WHERE ticker=? ORDER BY trade_date DESC LIMIT ?",
            (benchmark.ticker, window - 1),
        )
        closes = [float(row["close"] or 0) for row in rows]
        if len(closes) < window - 1:
            return None
        return benchmark.last > statistics.fmean([benchmark.last, *closes])

    def _variant_candidates(self, quotes: list[Quote]) -> tuple[dict[str, list[tuple[Quote, float]]], dict[str, str | None]]:
        by_ticker = {quote.ticker: quote for quote in quotes}
        benchmark = by_ticker.get(self.settings.strategy.benchmark_ticker)
        scores: list[tuple[Quote, float]] = []
        for ticker in self.settings.strategy.mag7_universe:
            quote = by_ticker.get(ticker)
            if quote is None or quote.last < self.settings.strategy.min_price or quote.halted:
                continue
            score = self._turnover_zscore(quote)
            if score is not None:
                scores.append((quote, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        aggressive_gate = self._benchmark_above_sma(benchmark, 200) if benchmark else None
        robust_gate = self._benchmark_above_sma(benchmark, 100) if benchmark else None
        robust_scores = [
            item for item in scores
            if item[0].session_open is not None and item[0].last > item[0].session_open
        ]
        candidates = {
            AGGRESSIVE: scores[:1] if aggressive_gate else [],
            ROBUST: robust_scores[:3] if robust_gate and len(robust_scores) >= 3 else [],
        }
        reasons = {
            AGGRESSIVE: None if candidates[AGGRESSIVE] else (
                "qqq_history_unavailable" if aggressive_gate is None else
                "qqq_below_sma200" if not aggressive_gate else "zscore_history_unavailable"
            ),
            ROBUST: None if candidates[ROBUST] else (
                "qqq_history_unavailable" if robust_gate is None else
                "qqq_below_sma100" if not robust_gate else "fewer_than_3_intraday_up"
            ),
        }
        return candidates, reasons

    async def freeze_signal_and_enter(self, session_date: date) -> None:
        async with self._lock:
            if self.state == TradingState.HALTED:
                return
            quotes = await self.snapshot_quotes(session_date)
            if self.settings.risk.dry_run:
                await self._freeze_variant_dry_runs(session_date, quotes)
                return
            ranked = self._rank(quotes)
            now = datetime.now(UTC)
            if len(ranked) < 3:
                await self._record_signal(session_date, ranked, False, "fewer_than_3_eligible")
                await self.skip_entry("fewer_than_3_eligible")
                return
            selected = ranked[0]
            self.transition(
                TradingState.SIGNAL_FROZEN, "turnover signal frozen", {"ticker": selected.ticker}
            )
            account = await self.broker.account()
            positions = await self.broker.positions()
            strategy_positions = self.strategy_positions(positions)
            self.db.execute(
                "INSERT INTO account_snapshot(ts_utc,net_liquidation,settled_cash,buying_power) VALUES(?,?,?,?)",
                (
                    account.timestamp.isoformat(),
                    account.net_liquidation,
                    account.settled_cash,
                    account.buying_power,
                ),
            )
            decision = self.risk.entry_decision(
                account,
                selected,
                strategy_positions,
                self.broker.is_connected(),
                self.account_synced,
                now,
            )
            await self._record_signal(
                session_date,
                ranked,
                decision.allowed,
                None if decision.allowed else decision.reason,
            )
            if not decision.allowed:
                await self.skip_entry(decision.reason)
                return
            request = OrderRequest(
                selected.ticker,
                "BUY",
                decision.shares,
                self.settings.execution.close_entry_mode,
                "DAY",
                decision.protected_price
                if self.settings.execution.close_entry_mode == "LOC"
                else None,
                f"turnover_top1:{session_date}:entry:{selected.ticker}:1",
            )
            await self._submit(session_date, "entry", request)

    async def _freeze_variant_dry_runs(self, session_date: date, quotes: list[Quote]) -> None:
        candidates, reasons = self._variant_candidates(quotes)
        now = datetime.now(UTC)
        any_orders = False
        for strategy_id, selected in candidates.items():
            payload = [
                {"ticker": quote.ticker, "zturnover120": score, "last": quote.last,
                 "session_open": quote.session_open}
                for quote, score in selected
            ]
            allowed = bool(selected) and self.settings.risk.trading_enabled and self.account_synced
            reason = reasons[strategy_id]
            if selected and not self.settings.risk.trading_enabled:
                reason = "trading_disabled"
            elif selected and not self.account_synced:
                reason = "account_not_synchronized"
            self.db.execute(
                "INSERT OR REPLACE INTO variant_signal VALUES(?,?,?,?,?,?,?)",
                (str(session_date), strategy_id, json.dumps([x[0].ticker for x in selected]),
                 now.isoformat(), json.dumps(payload), int(allowed), reason),
            )
            if not allowed:
                continue
            per_name_capital = self.settings.risk.initial_strategy_capital_usd / len(selected)
            for sequence, (quote, _) in enumerate(selected, 1):
                shares = math.floor(per_name_capital / quote.last)
                if shares < 1:
                    continue
                request = OrderRequest(
                    quote.ticker, "BUY", shares, "MOC", "DAY", None,
                    f"{strategy_id}:{session_date}:entry:{quote.ticker}:{sequence}",
                )
                await self._submit(session_date, f"entry:{strategy_id}", request)
                any_orders = True
        if any_orders:
            self.transition(TradingState.BUY_SUBMITTED, "dual-variant DRY_RUN MOC orders recorded")
        else:
            self.transition(TradingState.IDLE, "both DRY_RUN variants held cash")

    async def _record_signal(
        self, day: date, ranked: list[Quote], tradeable: bool, skip: str | None
    ) -> None:
        now = datetime.now(UTC)
        payload = [
            {
                "ticker": q.ticker,
                "last": q.last,
                "volume": q.cumulative_volume,
                "turnover": q.dollar_turnover,
            }
            for q in ranked
        ]
        self.db.execute(
            "INSERT OR REPLACE INTO strategy_signal(trade_date,selected_ticker,signal_ts_utc,signal_ts_ny,rank_json,is_tradeable,skip_reason) VALUES(?,?,?,?,?,?,?)",
            (
                str(day),
                ranked[0].ticker if ranked else None,
                now.isoformat(),
                now.astimezone(NY).isoformat(),
                json.dumps(payload),
                int(tradeable),
                skip,
            ),
        )

    async def _submit(
        self,
        day: date,
        leg: str,
        request: OrderRequest,
        *,
        force_real: bool = False,
    ) -> BrokerOrder | None:
        local_id = str(uuid.uuid4())
        now = utc_now()
        is_dry_run = self.settings.risk.dry_run and not force_real
        status = "DRY_RUN" if is_dry_run else "PendingSubmit"
        self.db.execute(
            "INSERT INTO strategy_order(local_order_id,trade_date,leg,ticker,action,order_type,tif,quantity,limit_price,order_ref,status,submitted_ts_utc,updated_ts_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                local_id,
                str(day),
                leg,
                request.ticker,
                request.action,
                request.order_type,
                request.tif,
                request.quantity,
                request.limit_price,
                request.order_ref,
                status,
                now,
                now,
            ),
        )
        if is_dry_run:
            self.transition(
                TradingState.BUY_SUBMITTED
                if request.action == "BUY"
                else TradingState.SELL_SUBMITTED,
                "dry-run order recorded",
                {"order_ref": request.order_ref},
            )
            return None
        try:
            order = await self.broker.place_order(request)
            self.db.execute(
                "UPDATE strategy_order SET ib_order_id=?,ib_perm_id=?,status=?,updated_ts_utc=? WHERE local_order_id=?",
                (order.order_id, order.perm_id, order.status, utc_now(), local_id),
            )
            self.transition(
                TradingState.BUY_SUBMITTED
                if request.action == "BUY"
                else TradingState.SELL_SUBMITTED,
                "order submitted",
                {"order_ref": request.order_ref},
            )
            return order
        except Exception as exc:  # noqa: BLE001 - every broker rejection must be persisted
            self.db.execute(
                "UPDATE strategy_order SET status='Rejected',reject_reason=?,updated_ts_utc=? WHERE local_order_id=?",
                (str(exc), utc_now(), local_id),
            )
            self.transition(
                TradingState.BUY_REJECTED
                if leg in {"entry", "manual_entry"}
                else TradingState.EXIT_FAILED,
                "order rejected",
            )
            await self.alerts.send(
                "order_rejected", "CRITICAL", f"订单被拒: {exc}", {"order_ref": request.order_ref}
            )
            return None

    def automatic_real_exposure(self) -> dict[str, float]:
        """Track only scheduled strategy fills; manual REAL trades are intentionally excluded."""
        rows = self.db.query(
            "SELECT order_ref,ticker,quantity FROM execution_ledger "
            "WHERE order_ref LIKE 'turnover_top1:%' ORDER BY fill_ts_utc"
        )
        exposure: dict[str, float] = {}
        for row in rows:
            parts = str(row["order_ref"]).split(":")
            if len(parts) < 3 or parts[2] not in {"entry", "exit", "force_exit"}:
                continue
            ticker = str(row["ticker"])
            quantity = float(row["quantity"])
            change = quantity if parts[2] == "entry" else -quantity
            exposure[ticker] = max(0.0, exposure.get(ticker, 0.0) + change)
        return {ticker: quantity for ticker, quantity in exposure.items() if quantity > 0}

    async def _automatic_real_positions(self) -> list[tuple[Position, int]]:
        await self.sync_execution_fills()
        exposure = self.automatic_real_exposure()
        positions = {position.ticker: position for position in await self.broker.positions()}
        result = []
        for ticker, tracked_quantity in exposure.items():
            position = positions.get(ticker)
            if position is None or position.quantity <= 0:
                continue
            quantity = math.floor(min(float(position.quantity), tracked_quantity))
            if quantity > 0:
                result.append((position, quantity))
        return result

    async def submit_open_exit(self, session_date: date) -> None:
        if self.settings.risk.dry_run:
            variant_positions = self.db.query("SELECT * FROM variant_dry_run_position")
            if variant_positions:
                for position in variant_positions:
                    request = OrderRequest(
                        position["ticker"], "SELL", int(position["quantity"]), "MKT", "DAY", None,
                        f"{position['strategy_id']}:{session_date}:dry_exit:{position['ticker']}:1",
                    )
                    await self._submit(session_date, f"exit:{position['strategy_id']}", request)
                return
            simulated = self.db.query("SELECT * FROM dry_run_position WHERE id=1")
            if simulated:
                position = simulated[0]
                request = OrderRequest(
                    position["ticker"],
                    "SELL",
                    int(position["quantity"]),
                    "MKT",
                    "DAY",
                    None,
                    f"turnover_top1:{session_date}:dry_exit:{position['ticker']}:1",
                )
                await self._submit(session_date, "exit", request)

            if not simulated:
                self.transition(TradingState.FLAT_CONFIRMED, "no DRY_RUN overnight position")
            return
        automatic_positions = await self._automatic_real_positions()
        if not automatic_positions:
            self.transition(TradingState.FLAT_CONFIRMED, "no automatic overnight position")
            return
        if len(automatic_positions) != 1:
            self.transition(TradingState.HALTED, "multiple strategy positions")
            await self.alerts.send("unexpected_position", "CRITICAL", "检测到多个策略持仓，已停机")
            return
        position, quantity = automatic_positions[0]
        request = OrderRequest(
            position.ticker,
            "SELL",
            quantity,
            "MKT",
            "DAY",
            None,
            f"turnover_top1:{session_date}:exit:{position.ticker}:1",
        )
        await self._submit(session_date, "exit", request)

    async def verify_exit(self, force: bool = False) -> None:
        if self.settings.risk.dry_run:
            variant_positions = self.db.query("SELECT * FROM variant_dry_run_position")
            if variant_positions:
                tickers = list({row["ticker"] for row in variant_positions})
                prices = {
                    quote.ticker: price
                    for quote in await self.broker.quotes(tickers)
                    if (price := self._opposing_price(quote, "SELL")) is not None
                }
                for position in variant_positions:
                    strategy_id = position["strategy_id"]
                    price = prices.get(position["ticker"])
                    if price is None:
                        self.db.event(
                            "WARNING",
                            "dry_run_exit_bid_missing",
                            "DRY_RUN 开盘退出缺少有效买一价，未模拟成交",
                            {"strategy_id": strategy_id, "ticker": position["ticker"]},
                        )
                        continue
                    orders = self.db.query(
                        "SELECT * FROM strategy_order WHERE leg=? AND ticker=? AND status='DRY_RUN' ORDER BY submitted_ts_utc DESC LIMIT 1",
                        (f"exit:{strategy_id}", position["ticker"]),
                    )
                    if not orders:
                        continue
                    order = orders[0]
                    commission = self.dry_run_commission(position["quantity"])
                    self.db.execute(
                        "INSERT OR IGNORE INTO variant_dry_run_fill VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"sim:{order['local_order_id']}", strategy_id, order["order_ref"],
                         position["ticker"], "SELL", position["quantity"], price, commission, utc_now()),
                    )
                    self.db.execute("UPDATE strategy_order SET status='SIM_FILLED',updated_ts_utc=? WHERE local_order_id=?", (utc_now(), order["local_order_id"]),)
                    self.db.execute("DELETE FROM variant_dry_run_position WHERE strategy_id=? AND ticker=?", (strategy_id, position["ticker"]),)
                self.latest_dry_run = await self.snapshot_dry_run_performance()
                if not self.db.query("SELECT 1 FROM variant_dry_run_position LIMIT 1"):
                    self.transition(TradingState.FLAT_CONFIRMED, "both DRY_RUN variants are flat")
                return
            simulated = self.db.query("SELECT * FROM dry_run_position WHERE id=1")
            if simulated:
                position = simulated[0]
                quotes = await self.broker.quotes([position["ticker"]])
                if not quotes:
                    await self.alerts.send(
                        "dry_run_exit_quote_missing", "WARNING", "DRY_RUN 开盘退出缺少行情"
                    )
                else:
                    order_rows = self.db.query(
                        "SELECT * FROM strategy_order WHERE leg='exit' AND action='SELL' "
                        "AND status='DRY_RUN' ORDER BY submitted_ts_utc DESC LIMIT 1"
                    )
                    if order_rows:
                        order = order_rows[0]
                        price = self._opposing_price(quotes[0], "SELL")
                        if price is None:
                            await self.alerts.send(
                                "dry_run_exit_bid_missing",
                                "WARNING",
                                "DRY_RUN 开盘退出缺少有效买一价，未模拟成交",
                                {"ticker": position["ticker"]},
                            )
                            return
                        commission = self.dry_run_commission(position["quantity"])
                        self.db.execute(
                            "INSERT OR IGNORE INTO dry_run_fill VALUES(?,?,?,?,?,?,?,?)",
                            (
                                f"sim:{order['local_order_id']}",
                                order["order_ref"],
                                position["ticker"],
                                "SELL",
                                position["quantity"],
                                price,
                                commission,
                                utc_now(),
                            ),
                        )
                        self.db.execute(
                            "UPDATE strategy_order SET status='SIM_FILLED',updated_ts_utc=? "
                            "WHERE local_order_id=?",
                            (utc_now(), order["local_order_id"]),
                        )
                        self.db.execute("DELETE FROM dry_run_position WHERE id=1")
                        self.latest_dry_run = await self.snapshot_dry_run_performance()

            dry_run_open = bool(self.db.query("SELECT 1 FROM dry_run_position WHERE id=1"))
            if not dry_run_open:
                self.transition(TradingState.FLAT_CONFIRMED, "DRY_RUN position is flat")
            return
        automatic_positions = await self._automatic_real_positions()
        if not automatic_positions:
            self.transition(TradingState.FLAT_CONFIRMED, "IB confirms automatic strategy flat")
            return
        await self.alerts.send(
            "open_exit_pending",
            "CRITICAL" if force else "WARNING",
            "开盘后策略持仓仍未清空",
            {
                "positions": [
                    {"ticker": position.ticker, "quantity": quantity}
                    for position, quantity in automatic_positions
                ]
            },
        )
        if force:
            trade_day = datetime.now(NY).date()
            for position, quantity in automatic_positions:
                request = OrderRequest(
                    position.ticker,
                    "SELL",
                    quantity,
                    "MKT",
                    "DAY",
                    None,
                    f"turnover_top1:{trade_day}:force_exit:{position.ticker}:1",
                )
                await self._submit(trade_day, "force_exit", request)

    async def verify_entry(self) -> None:
        if self.settings.risk.dry_run:
            variant_orders = self.db.query(
                "SELECT * FROM strategy_order WHERE action='BUY' AND status='DRY_RUN' "
                "AND (order_ref LIKE ? OR order_ref LIKE ?) ORDER BY submitted_ts_utc",
                (f"{AGGRESSIVE}:%", f"{ROBUST}:%"),
            )
            if variant_orders:
                prices = {
                    quote.ticker: price
                    for quote in await self.broker.quotes(
                        list({order["ticker"] for order in variant_orders})
                    )
                    if (price := self._opposing_price(quote, "BUY")) is not None
                }
                filled = 0
                for order in variant_orders:
                    price = prices.get(order["ticker"])
                    if price is None:
                        self.db.event(
                            "WARNING",
                            "dry_run_entry_ask_missing",
                            "DRY_RUN 收盘入场缺少有效卖一价，未模拟成交",
                            {"order_ref": order["order_ref"], "ticker": order["ticker"]},
                        )
                        continue
                    strategy_id = str(order["order_ref"]).split(":", 1)[0]
                    commission = self.dry_run_commission(order["quantity"])
                    self.db.execute(
                        "INSERT OR IGNORE INTO variant_dry_run_fill VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"sim:{order['local_order_id']}", strategy_id, order["order_ref"],
                         order["ticker"], "BUY", order["quantity"], price, commission, utc_now()),
                    )
                    self.db.execute(
                        "INSERT OR REPLACE INTO variant_dry_run_position VALUES(?,?,?,?,?,?,?)",
                        (strategy_id, order["ticker"], order["quantity"], price, price, utc_now(), order["order_ref"]),
                    )
                    self.db.execute("UPDATE strategy_order SET status='SIM_FILLED',updated_ts_utc=? WHERE local_order_id=?", (utc_now(), order["local_order_id"]),)
                    filled += 1
                self.latest_dry_run = await self.snapshot_dry_run_performance()
                if filled:
                    self.transition(TradingState.HOLD_OVERNIGHT, "both DRY_RUN variants MOC-filled")
                return
            orders = self.db.query(
                "SELECT * FROM strategy_order WHERE action='BUY' AND status='DRY_RUN' ORDER BY submitted_ts_utc DESC LIMIT 1"
            )
            if not orders:
                return
            order = orders[0]
            quotes = await self.broker.quotes([order["ticker"]])
            if not quotes:
                self.transition(TradingState.BUY_NOT_FILLED, "DRY_RUN entry quote unavailable")
                return
            price = self._opposing_price(quotes[0], "BUY")
            if price is None:
                self.transition(TradingState.BUY_NOT_FILLED, "DRY_RUN entry ask unavailable")
                await self.alerts.send(
                    "dry_run_entry_ask_missing",
                    "WARNING",
                    "DRY_RUN 收盘入场缺少有效卖一价，未模拟成交",
                    {"ticker": order["ticker"]},
                )
                return
            can_fill = order["order_type"] == "MOC" or (
                order["limit_price"] is not None and price <= float(order["limit_price"])
            )
            if not can_fill:
                self.db.execute(
                    "UPDATE strategy_order SET status='SIM_NOT_FILLED',updated_ts_utc=? WHERE local_order_id=?",
                    (utc_now(), order["local_order_id"]),
                )
                self.transition(TradingState.BUY_NOT_FILLED, "DRY_RUN LOC price not reached")
                return
            commission = self.dry_run_commission(order["quantity"])
            self.db.execute(
                "INSERT OR IGNORE INTO dry_run_fill VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"sim:{order['local_order_id']}",
                    order["order_ref"],
                    order["ticker"],
                    "BUY",
                    order["quantity"],
                    price,
                    commission,
                    utc_now(),
                ),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO dry_run_position VALUES(1,?,?,?,?,?,?)",
                (
                    order["ticker"],
                    order["quantity"],
                    price,
                    price,
                    utc_now(),
                    order["order_ref"],
                ),
            )
            self.db.execute(
                "UPDATE strategy_order SET status='SIM_FILLED',updated_ts_utc=? WHERE local_order_id=?",
                (utc_now(), order["local_order_id"]),
            )
            self.latest_dry_run = await self.snapshot_dry_run_performance()
            self.transition(TradingState.HOLD_OVERNIGHT, "DRY_RUN entry filled")
            return
        positions = [
            p for p in await self.broker.positions() if p.ticker in self.settings.strategy.universe
        ]
        if positions:
            self.position_cache = positions
            ts = utc_now()
            for p in positions:
                self.db.execute(
                    "INSERT INTO position_snapshot VALUES(?,?,?,?)",
                    (ts, p.ticker, p.quantity, p.average_cost),
                )
            self.transition(TradingState.HOLD_OVERNIGHT, "entry position confirmed")
        elif self.state == TradingState.BUY_SUBMITTED:
            self.transition(TradingState.BUY_NOT_FILLED, "no entry position after close")

    async def skip_entry(self, reason: str) -> None:
        self.db.event("WARNING", "entry_skipped", reason)
        self.transition(TradingState.IDLE, f"entry skipped: {reason}")
