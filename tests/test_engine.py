from datetime import UTC, date, datetime

import pytest
from conftest import FakeBroker

from ibtrader.alerts import AlertManager
from ibtrader.config import Settings
from ibtrader.db import Database
from ibtrader.engine import TradingEngine
from ibtrader.models import ExecutionFill, Position


def seed_history(db: Database, tickers: list[str]) -> None:
    for ticker in [*tickers, "QQQ"]:
        for day in range(1, 200):
            db.execute(
                "INSERT INTO ohlcv(ticker,trade_date,open,high,low,close,vol,source) VALUES(?,?,?,?,?,?,?,?)",
                (ticker, f"{day:04d}", 90, 101, 89, 90, 1_000_000 + day * 1000, "test"),
            )


@pytest.mark.asyncio
async def test_dry_run_records_order_without_broker_submission(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    seed_history(db, settings.strategy.universe)
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    engine.account_synced = True
    await engine.freeze_signal_and_enter(date(2026, 8, 31))
    orders = db.query("SELECT * FROM strategy_order")
    assert len(orders) == 4
    assert {order["status"] for order in orders} == {"DRY_RUN"}
    assert {order["order_type"] for order in orders} == {"MOC"}
    assert broker.placed == []


@pytest.mark.asyncio
async def test_both_documented_variants_complete_independent_dry_run_cycle(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    seed_history(db, settings.strategy.universe)
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    engine.account_synced = True

    await engine.freeze_signal_and_enter(date(2026, 8, 31))
    await engine.verify_entry()

    positions = db.query(
        "SELECT strategy_id,ticker,quantity FROM variant_dry_run_position ORDER BY strategy_id,ticker"
    )
    assert len(positions) == 4
    assert len({row["strategy_id"] for row in positions}) == 2
    assert all(row["quantity"] > 0 for row in positions)
    assert sum(row["strategy_id"].endswith("sma100") for row in positions) == 3

    await engine.submit_open_exit(date(2026, 9, 1))
    await engine.verify_exit()

    assert db.query("SELECT * FROM variant_dry_run_position") == []
    fills = db.query("SELECT action,price FROM variant_dry_run_fill")
    assert len(fills) == 8
    assert {row["price"] for row in fills if row["action"] == "BUY"} == {101.0}
    assert {row["price"] for row in fills if row["action"] == "SELL"} == {99.0}
    assert broker.placed == []


@pytest.mark.asyncio
async def test_disabled_trading_records_skipped_signal(tmp_path):
    settings = Settings()
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    seed_history(db, settings.strategy.universe)
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    engine.account_synced = True
    await engine.freeze_signal_and_enter(date(2026, 8, 31))
    assert {row["skip_reason"] for row in db.query("SELECT skip_reason FROM variant_signal")} == {"trading_disabled"}
    assert db.query("SELECT * FROM strategy_order") == []


@pytest.mark.asyncio
async def test_missing_history_blocks_entry(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    engine.account_synced = True
    await engine.freeze_signal_and_enter(date(2026, 8, 31))
    assert {row["skip_reason"] for row in db.query("SELECT skip_reason FROM variant_signal")} == {"qqq_history_unavailable"}


@pytest.mark.asyncio
async def test_non_strategy_position_does_not_block_entry(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    seed_history(db, settings.strategy.universe)
    broker = FakeBroker()
    broker.position_list = [Position("BRK.B", 2, 500)]
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    engine.account_synced = True
    await engine.freeze_signal_and_enter(date(2026, 8, 31))
    assert {row["status"] for row in db.query("SELECT status FROM strategy_order")} == {"DRY_RUN"}


def test_strategy_pnl_includes_ib_commission(tmp_path):
    settings = Settings()
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    engine = TradingEngine(settings, db, FakeBroker(), AlertManager(settings.alerts, db))
    db.execute(
        "INSERT INTO execution_ledger VALUES(?,?,?,?,?,?,?,?)",
        ("buy", "turnover_top1:x", "AAPL", "BOT", 10, 100, 1.25, "2026-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO execution_ledger VALUES(?,?,?,?,?,?,?,?)",
        ("sell", "turnover_top1:y", "AAPL", "SLD", 10, 110, 1.25, "2026-01-02T00:00:00Z"),
    )
    pnl, fees = engine.strategy_pnl(0)
    assert pnl == 97.5
    assert fees == 2.5


@pytest.mark.asyncio
async def test_reconcile_ignores_non_strategy_positions(tmp_path):
    settings = Settings()
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    broker.position_list = [Position("BRK.B", 2, 500, 510, 1020, 20)]
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    await engine.reconcile()
    assert engine.state.value != "HALTED"
    assert engine.position_cache == []
    assert engine.all_position_cache[0].ticker == "BRK.B"


@pytest.mark.asyncio
async def test_portfolio_marks_strategy_scope(tmp_path):
    settings = Settings()
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    broker.position_list = [
        Position("AAPL", 10, 100, 105, 1050, 50),
        Position("BRK.B", 2, 500, 510, 1020, 20),
    ]
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    snapshot = await engine.snapshot_portfolio()
    assert snapshot["strategy_market_value"] == 1050
    assert [item["is_strategy"] for item in snapshot["positions"]] == [True, False]


@pytest.mark.asyncio
async def test_dry_run_entry_and_exit_produce_separate_pnl(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    db.execute(
        "INSERT INTO strategy_order(local_order_id,trade_date,leg,ticker,action,order_type,tif,quantity,limit_price,order_ref,status,submitted_ts_utc,updated_ts_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "entry",
            "2026-08-31",
            "entry",
            "AAPL",
            "BUY",
            "LOC",
            "DAY",
            10,
            101,
            "turnover_top1:2026-08-31:entry:AAPL:1",
            "DRY_RUN",
            "2026-08-31T19:45:00Z",
            "2026-08-31T19:45:00Z",
        ),
    )
    await engine.verify_entry()
    assert db.query("SELECT ticker,quantity FROM dry_run_position") == [
        {"ticker": "AAPL", "quantity": 10}
    ]
    await engine.submit_open_exit(date(2026, 9, 1))
    assert db.query(
        "SELECT order_type,tif FROM strategy_order WHERE leg='exit' ORDER BY submitted_ts_utc DESC LIMIT 1"
    ) == [{"order_type": "MKT", "tif": "DAY"}]
    await engine.verify_exit()
    assert db.query("SELECT * FROM dry_run_position") == []
    snapshot = await engine.snapshot_dry_run_performance()
    assert snapshot["pnl"] == -22.0


@pytest.mark.asyncio
async def test_manual_real_order_is_blocked_by_default(tmp_path):
    settings = Settings()
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    engine = TradingEngine(settings, db, FakeBroker(), AlertManager(settings.alerts, db))
    with pytest.raises(RuntimeError, match="trading_enabled"):
        await engine.manual_real_order("AAPL", "BUY", 1, "LOC")


@pytest.mark.asyncio
async def test_manual_real_buy_bypasses_only_global_dry_run(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    settings.risk.manual_real_order_enabled = True
    settings.ib.readonly_mode = False
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))

    order = await engine.manual_real_order("AAPL", "BUY", 2, "MOC")

    assert order is not None
    assert [(request.action, request.quantity) for request in broker.placed] == [("BUY", 2)]
    assert db.query("SELECT leg,status FROM strategy_order") == [
        {"leg": "manual_entry", "status": "Submitted"}
    ]


@pytest.mark.asyncio
async def test_scheduled_order_stays_dry_run_when_manual_real_is_enabled(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    settings.risk.manual_real_order_enabled = True
    settings.ib.readonly_mode = False
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    seed_history(db, settings.strategy.universe)
    broker = FakeBroker()
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))
    engine.account_synced = True

    await engine.freeze_signal_and_enter(date(2026, 8, 31))

    assert broker.placed == []
    assert len(db.query("SELECT leg,status FROM strategy_order")) == 4
    assert {row["status"] for row in db.query("SELECT leg,status FROM strategy_order")} == {"DRY_RUN"}


@pytest.mark.asyncio
async def test_manual_real_sell_is_allowed_but_cannot_open_short(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    settings.risk.manual_real_order_enabled = True
    settings.ib.readonly_mode = False
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    broker.position_list = [Position("AAPL", 5, 100)]
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))

    await engine.manual_real_order("AAPL", "SELL", 3, "LOC")
    assert broker.placed[0].action == "SELL"
    assert broker.placed[0].quantity == 3

    with pytest.raises(RuntimeError, match="exceeds the current long position"):
        await engine.manual_real_order("AAPL", "SELL", 6, "MOC")


@pytest.mark.asyncio
async def test_turnover_test_is_an_immediate_snapshot(tmp_path):
    settings = Settings()
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    engine = TradingEngine(settings, db, FakeBroker(), AlertManager(settings.alerts, db))

    result = await engine.start_turnover_test()

    assert result["selected_ticker"] == settings.strategy.universe[-1]
    assert result["market_data_mode"] == "LIVE"
    assert result["rank"][0]["volume"] == 9_000_000
    assert result["rank"][0]["market_data_mode"] == "LIVE"
    rows = db.query(
        "SELECT duration_seconds,interval_seconds,status,selected_ticker FROM turnover_test_run"
    )
    assert rows == [
        {
            "duration_seconds": 0,
            "interval_seconds": 0,
            "status": "COMPLETED",
            "selected_ticker": settings.strategy.universe[-1],
        }
    ]


@pytest.mark.asyncio
async def test_automatic_exit_explicitly_ignores_manual_real_position(tmp_path):
    settings = Settings()
    settings.risk.trading_enabled = True
    settings.risk.dry_run = False
    settings.ib.readonly_mode = False
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    broker = FakeBroker()
    broker.position_list = [
        Position("AAPL", 3, 100),
        Position("MSFT", 4, 100),
        Position("BRK.B", 2, 500),
    ]
    broker.execution_list = [
        ExecutionFill(
            "automatic-buy",
            "turnover_top1:2026-08-31:entry:AAPL:1",
            "AAPL",
            "BOT",
            3,
            100,
            1,
            datetime.now(UTC),
        ),
        ExecutionFill(
            "manual-buy",
            "turnover_top1:2026-08-31:manual_buy:MSFT:1",
            "MSFT",
            "BOT",
            4,
            100,
            1,
            datetime.now(UTC),
        ),
    ]
    engine = TradingEngine(settings, db, broker, AlertManager(settings.alerts, db))

    await engine.submit_open_exit(date(2026, 9, 1))

    assert [(request.ticker, request.action, request.quantity) for request in broker.placed] == [
        ("AAPL", "SELL", 3)
    ]
