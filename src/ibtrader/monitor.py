from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from .alerts import AlertManager
from .broker import BrokerAdapter
from .config import Settings
from .db import Database
from .models import HealthStatus


class GatewayMonitor:
    def __init__(
        self, settings: Settings, db: Database, broker: BrokerAdapter, alerts: AlertManager
    ):
        self.settings, self.db, self.broker, self.alerts = settings, db, broker, alerts
        self.status = HealthStatus()
        self._stopped = asyncio.Event()
        self._down_since: datetime | None = None

    async def run(self) -> None:
        attempt = 0
        backoffs = self.settings.ib.reconnect_backoff_seconds
        while not self._stopped.is_set():
            if not self.broker.is_connected():
                try:
                    await self.broker.connect()
                    attempt = 0
                except Exception as exc:  # noqa: BLE001 - reconnect loop handles vendor/network errors
                    attempt += 1
                    self.status = HealthStatus(False, message=str(exc), failures=attempt)
            if self.broker.is_connected():
                self.status = await self.broker.health()
            now = datetime.now(UTC)
            self.db.execute(
                "INSERT OR REPLACE INTO gateway_health VALUES(?,?,?,?,?)",
                (
                    now.isoformat(),
                    int(self.status.connected),
                    self.status.latency_ms,
                    self.status.clock_skew_seconds,
                    self.status.message,
                ),
            )
            if self.status.connected:
                if self._down_since:
                    await self.alerts.send("gateway_recovered", "WARNING", "IB Gateway 连接已恢复")
                self._down_since = None
                if (
                    self.status.clock_skew_seconds is not None
                    and self.status.clock_skew_seconds > 5
                ):
                    await self.alerts.send(
                        "clock_skew",
                        "CRITICAL",
                        "本机与 IB Server 时间偏差超过 5 秒",
                        {"seconds": self.status.clock_skew_seconds},
                    )
                await asyncio.sleep(10)
            else:
                self._down_since = self._down_since or now
                down_for = (now - self._down_since).total_seconds()
                if down_for >= self.settings.alerts.gateway_down_after_seconds:
                    await self.alerts.send(
                        "gateway_down", "CRITICAL", f"IB Gateway 已断线 {int(down_for)} 秒"
                    )
                await asyncio.sleep(backoffs[min(attempt, len(backoffs) - 1)])

    def stop(self) -> None:
        self._stopped.set()
