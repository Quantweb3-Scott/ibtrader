from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Settings
from .models import AccountSnapshot, Position, Quote


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    target_notional: float = 0
    shares: int = 0
    protected_price: float = 0


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def entry_decision(
        self,
        account: AccountSnapshot,
        quote: Quote,
        positions: list[Position],
        gateway_connected: bool,
        synchronized: bool,
        now: datetime | None = None,
    ) -> RiskDecision:
        cfg = self.settings.risk
        now = now or datetime.now(UTC)
        if not cfg.trading_enabled:
            return RiskDecision(False, "trading_disabled")
        if not gateway_connected:
            return RiskDecision(False, "gateway_disconnected")
        if not synchronized:
            return RiskDecision(False, "account_not_synchronized")
        if positions:
            return RiskDecision(False, "existing_position")
        age = (now - quote.timestamp.astimezone(UTC)).total_seconds()
        if age >= 5:
            return RiskDecision(False, "stale_market_data")
        if quote.halted or quote.last < self.settings.strategy.min_price:
            return RiskDecision(False, "untradeable_quote")
        available = min(
            cfg.initial_strategy_capital_usd,
            cfg.max_strategy_capital_usd,
            account.net_liquidation * cfg.max_account_exposure_pct,
            account.settled_cash - cfg.cash_reserve_usd,
            account.buying_power / cfg.max_leverage,
            cfg.max_single_order_notional_usd,
        )
        protected = quote.last * (1 + self.settings.execution.max_entry_slippage_bps / 10_000)
        shares = math.floor(max(0, available) / protected)
        notional = shares * protected
        if shares < 1 or notional < cfg.min_order_notional_usd:
            return RiskDecision(False, "insufficient_strategy_cash")
        if notional > cfg.max_position_notional_usd:
            return RiskDecision(False, "position_limit_exceeded")
        return RiskDecision(True, "approved", notional, shares, round(protected, 2))
