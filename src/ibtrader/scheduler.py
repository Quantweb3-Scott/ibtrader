from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .calendar import NY, MarketCalendar, Session
from .engine import TradingEngine

logger = logging.getLogger(__name__)


class TradingScheduler:
    def __init__(self, engine: TradingEngine, calendar: MarketCalendar):
        self.engine, self.calendar = engine, calendar
        self._stopped = asyncio.Event()
        self._completed: set[str] = set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(1)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        local_day = now.astimezone(NY).date()
        session = self.calendar.session(local_day)
        if session is None:
            return
        if session.early_close and not self.engine.settings.strategy.trade_early_close_days:
            await self._once(
                session,
                "early_close_skip",
                session.close_at - timedelta(minutes=30),
                self.engine.skip_entry,
                "early_close",
            )
            return
        triggers = self.calendar.triggers(session)
        await self._once(
            session,
            "submit_exit",
            triggers["submit_exit"],
            self.engine.submit_open_exit,
            session.trade_date,
        )
        await self._once(
            session, "fallback_exit", triggers["fallback_exit"], self.engine.verify_exit, False
        )
        await self._once(
            session, "force_exit", triggers["force_exit"], self.engine.verify_exit, True
        )
        await self._once(session, "preflight", triggers["preflight"], self.engine.preflight_close)
        await self._once(
            session,
            "snapshot",
            triggers["snapshot"],
            self.engine.snapshot_quotes,
            session.trade_date,
        )
        await self._once(
            session,
            "freeze_entry",
            triggers["freeze_entry"],
            self.engine.freeze_signal_and_enter,
            session.trade_date,
        )
        await self._once(
            session, "verify_entry", triggers["verify_entry"], self.engine.verify_entry
        )
        await self._once(session, "post_close", triggers["post_close"], self.engine.refresh_history)

    async def _once(self, session: Session, name: str, trigger: datetime, fn, *args) -> None:
        key = f"{session.trade_date}:{name}"
        now = datetime.now(UTC)
        trigger_utc = trigger.astimezone(UTC)
        if key not in self._completed and trigger_utc <= now < trigger_utc + timedelta(seconds=90):
            self._completed.add(key)
            await fn(*args)

    def stop(self) -> None:
        self._stopped.set()
