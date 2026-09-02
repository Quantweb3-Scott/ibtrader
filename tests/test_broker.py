import asyncio
import math

import pytest

from ibtrader.broker import IBGatewayAdapter
from ibtrader.config import IBConfig


class FakeTicker:
    def __init__(self, last, market_price, volume):
        self.last = last
        self._market_price = market_price
        self.volume = volume
        self.bid = math.nan
        self.ask = math.nan

    def marketPrice(self):
        return self._market_price


@pytest.mark.asyncio
async def test_quotes_discard_non_finite_prices():
    broker = IBGatewayAdapter(IBConfig())
    broker._tickers["AAPL"] = FakeTicker(math.nan, math.nan, 100)

    assert await broker.quotes(["AAPL"]) == []


@pytest.mark.asyncio
async def test_quotes_use_finite_market_price_and_zero_missing_volume():
    broker = IBGatewayAdapter(IBConfig())
    broker._tickers["AAPL"] = FakeTicker(math.nan, 100.0, math.nan)

    quotes = await broker.quotes(["AAPL"])

    assert len(quotes) == 1
    assert quotes[0].last == 100.0
    assert quotes[0].cumulative_volume == 0.0


@pytest.mark.asyncio
async def test_quotes_wait_for_all_new_subscriptions(monkeypatch):
    broker = IBGatewayAdapter(IBConfig(market_data_wait_seconds=0.5))
    broker._tickers = {
        "AAPL": FakeTicker(math.nan, math.nan, 100),
        "MSFT": FakeTicker(math.nan, math.nan, 200),
    }

    async def subscribe(_tickers):
        return {"AAPL", "MSFT"}

    async def publish_quotes():
        await asyncio.sleep(0.05)
        broker._tickers["AAPL"].last = 100.0
        await asyncio.sleep(0.05)
        broker._tickers["MSFT"].last = 200.0

    monkeypatch.setattr(broker, "_subscribe", subscribe)
    publisher = asyncio.create_task(publish_quotes())

    quotes = await broker.quotes(["AAPL", "MSFT"])
    await publisher

    assert [(quote.ticker, quote.last) for quote in quotes] == [
        ("AAPL", 100.0),
        ("MSFT", 200.0),
    ]


@pytest.mark.asyncio
async def test_quotes_return_partial_results_after_wait_timeout(monkeypatch):
    broker = IBGatewayAdapter(IBConfig(market_data_wait_seconds=0.05))
    broker._tickers = {
        "AAPL": FakeTicker(100.0, 100.0, 100),
        "MSFT": FakeTicker(math.nan, math.nan, 200),
    }

    async def subscribe(_tickers):
        return {"AAPL", "MSFT"}

    monkeypatch.setattr(broker, "_subscribe", subscribe)

    quotes = await broker.quotes(["AAPL", "MSFT"])

    assert [(quote.ticker, quote.last) for quote in quotes] == [("AAPL", 100.0)]


@pytest.mark.asyncio
async def test_turnover_waits_for_live_cumulative_volume():
    broker = IBGatewayAdapter(IBConfig(market_data_wait_seconds=0.5))
    broker._tickers["AAPL"] = FakeTicker(100.0, 100.0, math.nan)

    async def publish_volume():
        await asyncio.sleep(0.05)
        broker._tickers["AAPL"].volume = 12345

    publisher = asyncio.create_task(publish_volume())
    quotes, mode = await broker.turnover_test_quotes(["AAPL"])
    await publisher

    assert mode == "LIVE"
    assert quotes[0].cumulative_volume == 12345


@pytest.mark.asyncio
async def test_turnover_fails_instead_of_using_delayed_history():
    broker = IBGatewayAdapter(IBConfig(market_data_wait_seconds=0.05))
    broker._tickers["AAPL"] = FakeTicker(100.0, 100.0, math.nan)

    with pytest.raises(RuntimeError, match="real-time price or cumulative volume unavailable"):
        await broker.turnover_test_quotes(["AAPL"])
