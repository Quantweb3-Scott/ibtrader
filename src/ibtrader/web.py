from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .alerts import AlertManager
from .broker import IBGatewayAdapter
from .calendar import MarketCalendar
from .config import Settings
from .db import Database
from .engine import TradingEngine
from .monitor import GatewayMonitor
from .scheduler import TradingScheduler


class ManualOrderRequest(BaseModel):
    ticker: str
    action: str
    quantity: int = Field(ge=1)
    order_type: str
    limit_price: float | None = Field(default=None, gt=0)


def create_app(settings: Settings | None = None, broker=None) -> FastAPI:
    settings = settings or Settings.load()
    db = Database(settings.app.database_path)
    db.initialize()
    broker = broker or IBGatewayAdapter(settings.ib)
    alerts = AlertManager(settings.alerts, db)
    engine = TradingEngine(settings, db, broker, alerts)
    monitor = GatewayMonitor(settings, db, broker, alerts)
    scheduler = TradingScheduler(
        engine,
        MarketCalendar(
            exit_submit_after_open_seconds=settings.execution.exit_submit_after_open_seconds,
            fallback_exit_after_open_seconds=settings.execution.fallback_exit_after_open_seconds,
            force_exit_after_open_seconds=settings.execution.force_exit_after_open_seconds,
            signal_freeze_minutes_before_close=(
                settings.strategy.signal_freeze_minutes_before_close
            ),
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        monitor_task = asyncio.create_task(monitor.run(), name="gateway-monitor")
        history_task = asyncio.create_task(
            engine.bootstrap_history_when_connected(), name="history-bootstrap"
        )
        scheduler_task = asyncio.create_task(scheduler.run(), name="trading-scheduler")
        portfolio_task = asyncio.create_task(engine.portfolio_loop(), name="portfolio-monitor")
        try:
            await asyncio.sleep(0)
            if broker.is_connected():
                await engine.reconcile()
            yield
        finally:
            monitor.stop()
            scheduler.stop()
            engine.stop_portfolio_loop()
            monitor_task.cancel()
            history_task.cancel()
            scheduler_task.cancel()
            portfolio_task.cancel()
            await broker.disconnect()

    app = FastAPI(title="IBTrader Control", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.engine = settings, db, engine
    app.state.monitor, app.state.scheduler = monitor, scheduler
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = settings.app.api_token
        if expected and expected != "change-me" and authorization != f"Bearer {expected}":
            raise HTTPException(401, "invalid bearer token")

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/status")
    async def status():
        if settings.risk.dry_run and not engine.latest_dry_run:
            engine.latest_dry_run = await engine.snapshot_dry_run_performance()
        health = asdict(monitor.status)
        health["server_time"] = health["server_time"].isoformat() if health["server_time"] else None
        health["last_success"] = (
            health["last_success"].isoformat() if health["last_success"] else None
        )
        return {
            "gateway": health,
            "state": engine.state.value,
            "trading_enabled": settings.risk.trading_enabled,
            "dry_run": settings.risk.dry_run,
            "manual_real_order_enabled": settings.risk.manual_real_order_enabled,
            "ib_readonly_mode": settings.ib.readonly_mode,
            "api_token_required": bool(
                settings.app.api_token and settings.app.api_token != "change-me"
            ),
            "account_synced": engine.account_synced,
            "portfolio": engine.latest_portfolio,
            "dry_run_portfolio": engine.latest_dry_run,
            "quotes": [
                {
                    "ticker": q.ticker,
                    "last": q.last,
                    "volume": q.cumulative_volume,
                    "turnover": q.dollar_turnover,
                    "timestamp": q.timestamp.isoformat(),
                }
                for q in sorted(
                    engine.last_quotes.values(), key=lambda item: item.dollar_turnover, reverse=True
                )
            ],
        }

    @app.get("/api/orders")
    async def orders(limit: int = 100):
        return db.query(
            "SELECT * FROM strategy_order ORDER BY submitted_ts_utc DESC LIMIT ?",
            (min(limit, 500),),
        )

    @app.get("/api/events")
    async def events(limit: int = 100):
        return db.query("SELECT * FROM risk_event ORDER BY id DESC LIMIT ?", (min(limit, 500),))

    @app.get("/api/signals")
    async def signals(limit: int = 30):
        return db.query(
            "SELECT * FROM strategy_signal ORDER BY trade_date DESC LIMIT ?", (min(limit, 365),)
        )

    @app.get("/api/portfolio")
    async def portfolio():
        if broker.is_connected():
            return await engine.snapshot_portfolio()
        return engine.latest_portfolio

    @app.get("/api/portfolio/history")
    async def portfolio_history(limit: int = 500):
        rows = db.query(
            "SELECT ts_utc,net_liquidation,account_pnl,normalized_nav,strategy_market_value,strategy_unrealized_pnl FROM portfolio_snapshot ORDER BY ts_utc DESC LIMIT ?",
            (min(limit, 5000),),
        )
        return list(reversed(rows))

    @app.get("/api/dry-run/history")
    async def dry_run_history(limit: int = 500):
        rows = db.query(
            "SELECT * FROM dry_run_performance ORDER BY ts_utc DESC LIMIT ?",
            (min(limit, 5000),),
        )
        return list(reversed(rows))

    @app.get("/api/turnover-tests")
    async def turnover_tests(limit: int = 20):
        return db.query(
            "SELECT * FROM turnover_test_run ORDER BY started_ts_utc DESC LIMIT ?",
            (min(limit, 100),),
        )

    @app.post("/api/operations/turnover-test", dependencies=[Depends(require_token)])
    async def start_turnover_test():
        try:
            result = await engine.start_turnover_test()
            return {"ok": True, **result}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/operations/manual-real-order", dependencies=[Depends(require_token)])
    async def manual_real_order(request: ManualOrderRequest):
        try:
            order = await engine.manual_real_order(
                request.ticker,
                request.action,
                request.quantity,
                request.order_type,
                request.limit_price,
            )
            return {"ok": True, "order": asdict(order) if order else None}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/operations/reset-performance", dependencies=[Depends(require_token)])
    async def reset_performance(mode: str = "REAL"):
        mode = mode.upper()
        if mode == "DRY_RUN":
            await engine.reset_dry_run()
            engine.latest_dry_run = await engine.snapshot_dry_run_performance()
            return {"ok": True, "mode": mode, "portfolio": engine.latest_dry_run}
        if mode != "REAL":
            raise HTTPException(400, "mode must be REAL or DRY_RUN")
        if not broker.is_connected():
            raise HTTPException(503, "IB Gateway disconnected")
        return {"ok": True, "mode": mode, "portfolio": await engine.reset_performance()}

    @app.post("/api/operations/reconcile", dependencies=[Depends(require_token)])
    async def reconcile():
        await engine.reconcile()
        return {"ok": True, "state": engine.state.value}

    @app.post("/api/operations/test-alert", dependencies=[Depends(require_token)])
    async def test_alert():
        sent = await alerts.send("manual_test", "WARNING", "IBTrader 手工测试报警")
        return {"ok": True, "sent": sent}

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                health = asdict(monitor.status)
                for key in ("server_time", "last_success"):
                    health[key] = health[key].isoformat() if health[key] else None
                await websocket.send_json(
                    {
                        "gateway": health,
                        "state": engine.state.value,
                        "portfolio": engine.latest_portfolio,
                    }
                )
                await asyncio.sleep(2)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app
