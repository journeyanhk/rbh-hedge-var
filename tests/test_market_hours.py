"""Trading-hours gate — weekly window evaluation (var-desgin6 Phase A)."""
import datetime

from rbh_hedge_var import market_hours

# Standard gold-spot week: open Sun 22:05 UTC, weekend close Fri 20:55 UTC.
HOURS = {"enabled": True, "open_windows": [{"open": "Sun 22:05", "close": "Fri 20:55"}]}


def test_disabled_is_always_open():
    r = market_hours.evaluate({"enabled": False}, datetime.datetime(2026, 9, 5, 12, 0))
    assert r["enabled"] is False and r["open"] is True


def test_enabled_but_no_windows_fails_closed():
    r = market_hours.evaluate({"enabled": True}, datetime.datetime(2026, 9, 4, 10, 0))
    assert r["enabled"] is True and r["open"] is False


def test_friday_midday_is_open():
    # 2026-09-04 is a Friday.
    r = market_hours.evaluate(HOURS, datetime.datetime(2026, 9, 4, 10, 19))
    assert r["open"] is True
    assert r["seconds_to_close"] == (10 * 60 + 36) * 60   # to Fri 20:55

def test_friday_near_close_small_seconds_to_close():
    r = market_hours.evaluate(HOURS, datetime.datetime(2026, 9, 4, 20, 40))
    assert r["open"] is True
    assert r["seconds_to_close"] == 15 * 60


def test_saturday_is_closed():
    r = market_hours.evaluate(HOURS, datetime.datetime(2026, 9, 5, 12, 0))
    assert r["open"] is False
    assert r["seconds_to_open"] is not None and r["seconds_to_open"] > 0


def test_sunday_evening_reopened():
    r = market_hours.evaluate(HOURS, datetime.datetime(2026, 9, 6, 23, 0))
    assert r["open"] is True


def test_multiple_windows_union_with_daily_break():
    # Two windows to illustrate a daily maintenance break is expressible.
    hours = {"enabled": True, "open_windows": [
        {"open": "Mon 00:00", "close": "Mon 21:00"},
        {"open": "Mon 22:00", "close": "Tue 21:00"},
    ]}
    inside = market_hours.evaluate(hours, datetime.datetime(2026, 9, 7, 10, 0))  # Mon
    assert inside["open"] is True
    in_break = market_hours.evaluate(hours, datetime.datetime(2026, 9, 7, 21, 30))
    assert in_break["open"] is False


# --- review17 P1-2: live-metadata conservative overlay parser ---------------

def _now():
    return datetime.datetime(2026, 9, 4, 12, 0, tzinfo=datetime.timezone.utc)


def test_metadata_close_epoch_seconds():
    now = _now()
    close = int(now.timestamp()) + 3600
    r = market_hours.next_close_from_metadata({"next_close_at": close}, now)
    assert r == 3600


def test_metadata_close_epoch_millis():
    now = _now()
    close_ms = (int(now.timestamp()) + 1800) * 1000
    r = market_hours.next_close_from_metadata({"next_close_at": close_ms}, now)
    assert r == 1800


def test_metadata_close_iso_string_nested():
    now = _now()
    r = market_hours.next_close_from_metadata(
        {"trading_schedule": {"next_close_at": "2026-09-04T13:00:00Z"}}, now)
    assert r == 3600


def test_metadata_close_absent_returns_none():
    assert market_hours.next_close_from_metadata({"unrelated": 1}, _now()) is None
    assert market_hours.next_close_from_metadata(None, _now()) is None
    assert market_hours.next_close_from_metadata({"next_close_at": "garbage"}, _now()) is None


def test_metadata_close_in_past_clamps_to_zero():
    now = _now()
    r = market_hours.next_close_from_metadata(
        {"next_close_at": int(now.timestamp()) - 500}, now)
    assert r == 0
