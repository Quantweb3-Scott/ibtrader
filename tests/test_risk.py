from datetime import UTC, datetime, timedelta

from ibtrader.config import Settings
from ibtrader.models import AccountSnapshot, Quote
from ibtrader.risk import RiskManager


def test_trading_is_locked_by_default():
    settings = Settings()
    quote = Quote("AAPL", 100, 1_000_000, 99, 101, datetime.now(UTC))
    account = AccountSnapshot(8500, 6000, 6000, datetime.now(UTC))
    assert (
        RiskManager(settings).entry_decision(account, quote, [], True, True).reason
        == "trading_disabled"
    )


def test_position_size_respects_capital_and_reserve():
    settings = Settings()
    settings.risk.trading_enabled = True
    quote = Quote("AAPL", 100, 1_000_000, 99, 101, datetime.now(UTC))
    account = AccountSnapshot(8500, 6000, 6000, datetime.now(UTC))
    result = RiskManager(settings).entry_decision(account, quote, [], True, True)
    assert result.allowed and result.shares == 29
    assert result.target_notional <= 3000


def test_stale_data_is_rejected():
    settings = Settings()
    settings.risk.trading_enabled = True
    quote = Quote("AAPL", 100, 1, 99, 101, datetime.now(UTC) - timedelta(seconds=6))
    account = AccountSnapshot(8500, 6000, 6000, datetime.now(UTC))
    assert (
        RiskManager(settings).entry_decision(account, quote, [], True, True).reason
        == "stale_market_data"
    )
