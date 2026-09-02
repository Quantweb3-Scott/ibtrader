from __future__ import annotations

import asyncio
import math
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from .config import IBConfig
from .models import (
    AccountSnapshot,
    BrokerOrder,
    ExecutionFill,
    HealthStatus,
    OrderRequest,
    Position,
    Quote,
)


def _ib_duration_days(calendar_days: int) -> str:
    """Format a calendar-day span as an IB durationStr, switching to years past 365 D."""
    if calendar_days <= 365:
        return f"{calendar_days} D"
    return f"{math.ceil(calendar_days / 365)} Y"


class BrokerAdapter(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    def is_connected(self) -> bool: ...
    @abstractmethod
    async def health(self) -> HealthStatus: ...
    @abstractmethod
    async def quotes(self, tickers: list[str]) -> list[Quote]: ...

    async def turnover_test_quotes(self, tickers: list[str]) -> tuple[list[Quote], str]:
        """Return quotes for a manual test. Production strategy calls quotes() directly."""
        return await self.quotes(tickers), "LIVE"

    @abstractmethod
    async def account(self) -> AccountSnapshot: ...
    @abstractmethod
    async def positions(self) -> list[Position]: ...
    @abstractmethod
    async def open_orders(self) -> list[BrokerOrder]: ...
    @abstractmethod
    async def place_order(self, request: OrderRequest) -> BrokerOrder: ...
    @abstractmethod
    async def cancel_order(self, order_id: int) -> None: ...

    @abstractmethod
    async def historical_daily_bars(self, ticker: str, days: int = 30) -> list[dict]: ...

    @abstractmethod
    async def execution_fills(self) -> list[ExecutionFill]: ...


class IBGatewayAdapter(BrokerAdapter):
    """All ib-async objects are confined to this adapter."""

    def __init__(self, config: IBConfig):
        self.config = config
        self._ib = None
        self._quote_contracts: dict[str, object] = {}
        self._tickers: dict[str, object] = {}

    @property
    def ib(self):
        if self._ib is None:
            from ib_async import IB

            self._ib = IB()
        return self._ib

    async def connect(self) -> None:
        if self.is_connected():
            return
        await self.ib.connectAsync(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
            account=self.config.account,
            readonly=self.config.readonly_mode,
            timeout=self.config.connect_timeout_seconds,
        )

    async def disconnect(self) -> None:
        if self._ib:
            self._ib.disconnect()

    def is_connected(self) -> bool:
        return bool(self._ib and self._ib.isConnected())

    async def health(self) -> HealthStatus:
        started = time.perf_counter()
        if not self.is_connected():
            return HealthStatus(connected=False, message="IB Gateway disconnected")
        try:
            server_time = await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), 5)
            now = datetime.now(UTC)
            if server_time.tzinfo is None:
                server_time = server_time.replace(tzinfo=UTC)
            latency = (time.perf_counter() - started) * 1000
            skew = abs((now - server_time.astimezone(UTC)).total_seconds())
            return HealthStatus(True, server_time, now, latency, skew, "healthy")
        except Exception as exc:  # noqa: BLE001 - normalize all vendor/network failures
            return HealthStatus(False, message=f"health check failed: {exc}")

    async def _subscribe(self, tickers: list[str]) -> set[str]:
        from ib_async import Stock

        missing = [symbol for symbol in tickers if symbol not in self._tickers]
        contracts = [Stock(symbol, "SMART", "USD") for symbol in missing]
        if contracts:
            qualified = await self.ib.qualifyContractsAsync(*contracts)
            for contract in qualified:
                self._quote_contracts[contract.symbol] = contract
                self._tickers[contract.symbol] = self.ib.reqMktData(contract, "", False, False)
        return set(missing)

    async def _wait_for_quotes(
        self, tickers: list[str], *, require_cumulative_volume: bool = False
    ) -> list[Quote]:
        deadline = time.monotonic() + self.config.market_data_wait_seconds
        while True:
            quotes = self._current_quotes(
                tickers, require_cumulative_volume=require_cumulative_volume
            )
            if len(quotes) == len(tickers) or time.monotonic() >= deadline:
                return quotes
            await asyncio.sleep(0.1)

    async def quotes(self, tickers: list[str]) -> list[Quote]:
        newly_subscribed = await self._subscribe(tickers)
        if newly_subscribed:
            return await self._wait_for_quotes(tickers)
        return self._current_quotes(tickers)

    def _current_quotes(
        self, tickers: list[str], *, require_cumulative_volume: bool = False
    ) -> list[Quote]:
        now = datetime.now(UTC)
        result = []
        for symbol in tickers:
            ticker = self._tickers.get(symbol)
            if not ticker:
                continue
            last = _number(ticker.last)
            if last is None:
                last = _number(ticker.marketPrice())
            volume = _number(ticker.volume)
            if require_cumulative_volume and volume is None:
                continue
            volume = volume if volume is not None else 0.0
            if last is None or last <= 0 or volume < 0:
                continue
            result.append(
                Quote(
                    symbol,
                    last,
                    volume,
                    _number(ticker.bid),
                    _number(ticker.ask),
                    now,
                    False,
                    _number(getattr(ticker, "open", None)),
                )
            )
        return result

    async def turnover_test_quotes(self, tickers: list[str]) -> tuple[list[Quote], str]:
        await self._subscribe(tickers)
        quotes = await self._wait_for_quotes(tickers, require_cumulative_volume=True)
        received = {quote.ticker for quote in quotes}
        missing = [ticker for ticker in tickers if ticker not in received]
        if missing:
            raise RuntimeError(
                "real-time price or cumulative volume unavailable for: " + ", ".join(missing)
            )
        return quotes, "LIVE"

    async def account(self) -> AccountSnapshot:
        values = await self.ib.accountSummaryAsync(self.config.account)
        by_tag = {
            v.tag: float(v.value)
            for v in values
            if v.currency in {"USD", "BASE"} and _is_number(v.value)
        }
        return AccountSnapshot(
            by_tag.get("NetLiquidation", 0),
            by_tag.get("SettledCash", by_tag.get("TotalCashValue", 0)),
            by_tag.get("BuyingPower", 0),
            datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        return [
            Position(
                item.contract.symbol,
                float(item.position),
                float(item.averageCost),
                _number(item.marketPrice),
                _number(item.marketValue),
                _number(item.unrealizedPNL),
            )
            for item in self.ib.portfolio(self.config.account)
            if item.contract.secType == "STK" and item.position
        ]

    async def open_orders(self) -> list[BrokerOrder]:
        return [self._trade_to_order(trade) for trade in self.ib.openTrades()]

    async def place_order(self, request: OrderRequest) -> BrokerOrder:
        from ib_async import LimitOrder, MarketOrder, Order, Stock

        contract = self._quote_contracts.get(request.ticker) or Stock(
            request.ticker, "SMART", "USD"
        )
        qualified = await self.ib.qualifyContractsAsync(contract)
        contract = qualified[0]
        if request.order_type == "LOC":
            order = Order(
                action=request.action,
                totalQuantity=request.quantity,
                orderType="LOC",
                lmtPrice=request.limit_price,
                tif="DAY",
            )
        elif request.order_type == "MOC":
            order = MarketOrder(request.action, request.quantity, tif="DAY")
            order.orderType = "MOC"
        elif request.order_type == "LOO":
            order = LimitOrder(request.action, request.quantity, request.limit_price, tif="OPG")
        elif request.order_type == "LMT":
            order = LimitOrder(
                request.action, request.quantity, request.limit_price, tif=request.tif or "DAY"
            )
        elif request.order_type == "MKT":
            order = MarketOrder(request.action, request.quantity, tif=request.tif or "DAY")
        else:
            raise ValueError(f"unsupported order type: {request.order_type}")
        order.orderRef = request.order_ref
        order.account = self.config.account
        trade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(0.25)
        return self._trade_to_order(trade, request)

    async def cancel_order(self, order_id: int) -> None:
        for trade in self.ib.openTrades():
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                return

    async def historical_daily_bars(self, ticker: str, days: int = 30) -> list[dict]:
        from ib_async import Stock

        contract = self._quote_contracts.get(ticker) or Stock(ticker, "SMART", "USD")
        qualified = await self.ib.qualifyContractsAsync(contract)
        bars = await self.ib.reqHistoricalDataAsync(
            qualified[0],
            endDateTime="",
            # `days` means trading bars. Request a calendar-day buffer for weekends/holidays.
            # IB rejects day-based durations longer than 365 D; those must use "Y".
            durationStr=_ib_duration_days(max(math.ceil(days * 1.7), 30)),
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=False,
        )
        return [
            {
                "trade_date": str(bar.date),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "vol": bar.volume,
            }
            for bar in bars
        ][-days:]

    async def execution_fills(self) -> list[ExecutionFill]:
        result = []
        fills = await self.ib.reqExecutionsAsync()
        for fill in fills:
            order_ref = fill.execution.orderRef or ""
            if not order_ref.startswith("turnover_top1:"):
                continue
            commission = _number(fill.commissionReport.commission) or 0.0
            result.append(
                ExecutionFill(
                    fill.execution.execId,
                    order_ref,
                    fill.contract.symbol,
                    fill.execution.side,
                    float(fill.execution.shares),
                    float(fill.execution.price),
                    commission,
                    fill.time.astimezone(UTC),
                )
            )
        return result

    def _trade_to_order(self, trade, request: OrderRequest | None = None) -> BrokerOrder:
        request = request or OrderRequest(
            trade.contract.symbol,
            trade.order.action,
            int(trade.order.totalQuantity),
            trade.order.orderType,
            trade.order.tif,
            _number(trade.order.lmtPrice),
            trade.order.orderRef or "",
        )
        return BrokerOrder(
            trade.order.orderId,
            trade.order.permId,
            trade.orderStatus.status,
            request,
            float(trade.orderStatus.filled),
            float(trade.orderStatus.remaining),
            _number(trade.orderStatus.avgFillPrice),
            datetime.now(UTC),
        )


def _is_number(value: object) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _number(value: object) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result != 1.7976931348623157e308 else None
    except (TypeError, ValueError):
        return None
