from datetime import UTC, datetime

from ibtrader.broker import BrokerAdapter
from ibtrader.models import (
    AccountSnapshot,
    BrokerOrder,
    ExecutionFill,
    HealthStatus,
    OrderRequest,
    Position,
    Quote,
)


class FakeBroker(BrokerAdapter):
    def __init__(self):
        self.connected = True
        self.position_list: list[Position] = []
        self.placed: list[OrderRequest] = []
        self.open_order_list: list[BrokerOrder] = []
        self.execution_list: list[ExecutionFill] = []
        self.cancelled: list[int] = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def health(self):
        return HealthStatus(self.connected, datetime.now(UTC), datetime.now(UTC), 1, 0, "healthy")

    async def quotes(self, tickers):
        return [
            Quote(t, 100 + i, 1_000_000 * (i + 1), 99, 101, datetime.now(UTC))
            for i, t in enumerate(tickers)
        ]

    async def account(self):
        return AccountSnapshot(8500, 6000, 6000, datetime.now(UTC))

    async def positions(self):
        return self.position_list

    async def open_orders(self):
        return self.open_order_list

    async def place_order(self, request):
        self.placed.append(request)
        return BrokerOrder(1, 2, "Submitted", request)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    async def historical_daily_bars(self, ticker, days=30):
        return []

    async def execution_fills(self):
        return self.execution_list
