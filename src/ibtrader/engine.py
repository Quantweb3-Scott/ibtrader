from __future__ import annotations

import asyncio
import json
import logging
import math
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

    async def reset_dry_run(self) -> None:
        self.db.execute("DELETE FROM dry_run_position")
        self.db.execute("DELETE FROM dry_run_fill")
        self.db.execute("DELETE FROM dry_run_performance")
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
        quotes = await self.broker.quotes(self.settings.strategy.universe)
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
        for ticker in self.settings.strategy.universe:
            try:
                bars = await self.broker.historical_daily_bars(ticker, 35)
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

    async def freeze_signal_and_enter(self, session_date: date) -> None:
        async with self._lock:
            if self.state == TradingState.HALTED:
                return
            quotes = await self.snapshot_quotes(session_date)
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
                        price = quotes[0].last
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
            price = quotes[0].last
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
