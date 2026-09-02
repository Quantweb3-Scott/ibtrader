from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Session:
    trade_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool


class MarketCalendar:
    def __init__(
        self,
        exit_submit_after_open_seconds: int = 60,
        fallback_exit_after_open_seconds: int = 120,
        force_exit_after_open_seconds: int = 300,
        signal_freeze_minutes_before_close: int = 16,
    ):
        self.calendar = xcals.get_calendar("XNYS")
        self.exit_submit_after_open_seconds = exit_submit_after_open_seconds
        self.fallback_exit_after_open_seconds = fallback_exit_after_open_seconds
        self.force_exit_after_open_seconds = force_exit_after_open_seconds
        self.signal_freeze_minutes_before_close = signal_freeze_minutes_before_close

    def session(self, day: date) -> Session | None:
        label = str(day)
        if not self.calendar.is_session(label):
            return None
        open_at = self.calendar.session_open(label).to_pydatetime().astimezone(NY)
        close_at = self.calendar.session_close(label).to_pydatetime().astimezone(NY)
        return Session(day, open_at, close_at, close_at.hour < 16)

    def next_session(self, after: date) -> Session:
        label = self.calendar.date_to_session(after, direction="next")
        if label.date() == after:
            label = self.calendar.next_session(label)
        return self.session(label.date())  # type: ignore[return-value]

    def triggers(self, session: Session) -> dict[str, datetime]:
        return {
            "prepare_exit": session.open_at - timedelta(minutes=10),
            "submit_exit": session.open_at + timedelta(seconds=self.exit_submit_after_open_seconds),
            "fallback_exit": session.open_at
            + timedelta(seconds=self.fallback_exit_after_open_seconds),
            "force_exit": session.open_at + timedelta(seconds=self.force_exit_after_open_seconds),
            "preflight": session.close_at - timedelta(minutes=30),
            "snapshot": session.close_at - timedelta(minutes=20),
            "freeze_entry": session.close_at
            - timedelta(minutes=self.signal_freeze_minutes_before_close),
            "verify_entry": session.close_at + timedelta(minutes=10),
            "post_close": session.close_at + timedelta(minutes=15),
        }


def utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
