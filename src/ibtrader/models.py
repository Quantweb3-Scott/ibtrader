from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class TradingState(StrEnum):
    IDLE = "IDLE"
    PREFLIGHT_CLOSE = "PREFLIGHT_CLOSE"
    SIGNAL_FROZEN = "SIGNAL_FROZEN"
    BUY_SUBMITTED = "BUY_SUBMITTED"
    BUY_FILLED = "BUY_FILLED"
    BUY_NOT_FILLED = "BUY_NOT_FILLED"
    BUY_REJECTED = "BUY_REJECTED"
    HOLD_OVERNIGHT = "HOLD_OVERNIGHT"
    SELL_SUBMITTED = "SELL_SUBMITTED"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    EXIT_FAILED = "EXIT_FAILED"
    POST_TRADE_RECONCILED = "POST_TRADE_RECONCILED"
    HALTED = "HALTED"


@dataclass(slots=True)
class Quote:
    ticker: str
    last: float
    cumulative_volume: float
    bid: float | None
    ask: float | None
    timestamp: datetime
    halted: bool = False

    @property
    def dollar_turnover(self) -> float:
        return self.last * self.cumulative_volume


@dataclass(slots=True)
class AccountSnapshot:
    net_liquidation: float
    settled_cash: float
    buying_power: float
    timestamp: datetime


@dataclass(slots=True)
class Position:
    ticker: str
    quantity: float
    average_cost: float
    market_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None


@dataclass(slots=True)
class OrderRequest:
    ticker: str
    action: str
    quantity: int
    order_type: str
    tif: str | None = None
    limit_price: float | None = None
    order_ref: str = ""


@dataclass(slots=True)
class BrokerOrder:
    order_id: int | None
    perm_id: int | None
    status: str
    request: OrderRequest
    filled: float = 0
    remaining: float = 0
    average_fill_price: float | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class ExecutionFill:
    execution_id: str
    order_ref: str
    ticker: str
    action: str
    quantity: float
    price: float
    commission: float
    timestamp: datetime


@dataclass(slots=True)
class HealthStatus:
    connected: bool = False
    server_time: datetime | None = None
    last_success: datetime | None = None
    latency_ms: float | None = None
    clock_skew_seconds: float | None = None
    message: str = "not checked"
    failures: int = 0
    details: dict = field(default_factory=dict)
