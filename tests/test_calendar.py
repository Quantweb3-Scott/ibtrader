from datetime import date

from ibtrader.calendar import MarketCalendar


def test_dst_is_derived_from_exchange_timezone():
    cal = MarketCalendar()
    winter = cal.session(date(2026, 1, 5))
    summer = cal.session(date(2026, 7, 6))
    assert winter and summer
    assert winter.open_at.hour == summer.open_at.hour == 9
    assert winter.open_at.utcoffset() != summer.open_at.utcoffset()


def test_thanksgiving_early_close():
    session = MarketCalendar().session(date(2026, 11, 27))
    assert session and session.early_close and session.close_at.hour == 13


def test_exit_is_submitted_after_opening_auction():
    calendar = MarketCalendar()
    session = calendar.session(date(2026, 9, 1))
    assert session
    triggers = calendar.triggers(session)
    assert (triggers["submit_exit"].hour, triggers["submit_exit"].minute) == (9, 31)
    assert (triggers["fallback_exit"].hour, triggers["fallback_exit"].minute) == (9, 32)
    assert (triggers["force_exit"].hour, triggers["force_exit"].minute) == (9, 35)
    assert (triggers["freeze_entry"].hour, triggers["freeze_entry"].minute) == (15, 44)
