"""Config-driven trading-hours gate for TradFi-scheduled venues (var-desgin6).

Variational's XAUS ("Swap on Gold Spot") follows gold spot TradFi hours: it
CLOSES over the weekend (and, optionally, for a short daily maintenance break).
While closed the RFQ does not fill, so a held position is FROZEN — stop-loss,
basis force-exit and the single-leg watchdog on the Variational leg all become
inert while the Lighter leg keeps trading 24/7. The only safe policy is to be
FLAT across any close.

This module is deliberately driven by an explicit weekly schedule in config
(UTC), not by guessing the shape of the venue's metadata: the schedule is the
one thing we must get provably right, and a config calendar is deterministic
and unit-testable. Live metadata may later be layered on as a MORE-conservative
overlay, never as the sole source of truth.

Schedule model: a list of weekly OPEN windows, each ``{"open": "Sun 22:05",
"close": "Fri 20:55"}`` in UTC. A window may wrap the week boundary (close
earlier in the week than open). The market is OPEN iff now falls in any window.
"""
from __future__ import annotations

from typing import Any

MINUTES_PER_WEEK = 7 * 24 * 60

_DOW = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _minute_of_week(token: str) -> int:
    """Parse 'Fri 20:55' (UTC) -> minute-of-week in [0, 10080). Mon 00:00 = 0."""
    parts = str(token).strip().split()
    if len(parts) != 2:
        raise ValueError(f"bad schedule token {token!r}; want 'Day HH:MM'")
    day, hhmm = parts
    dow = _DOW.get(day.strip().lower())
    if dow is None:
        raise ValueError(f"unknown weekday {day!r} in {token!r}")
    hh, _, mm = hhmm.partition(":")
    minute = int(hh) * 60 + int(mm or 0)
    if not (0 <= minute < 24 * 60):
        raise ValueError(f"bad time {hhmm!r} in {token!r}")
    return dow * 24 * 60 + minute


def _windows(cfg_hours: dict[str, Any]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for w in cfg_hours.get("open_windows") or []:
        try:
            out.append((_minute_of_week(w["open"]), _minute_of_week(w["close"])))
        except (KeyError, ValueError):
            continue
    return out


def _now_mow(now_utc) -> int:
    return now_utc.weekday() * 24 * 60 + now_utc.hour * 60 + now_utc.minute


def _contains(open_mow: int, close_mow: int, now_mow: int) -> tuple[bool, int]:
    """Return (is_inside, minutes_to_close) for one (possibly week-wrapping)
    open window. minutes_to_close is the forward distance now->close."""
    span = (close_mow - open_mow) % MINUTES_PER_WEEK        # window length
    offset = (now_mow - open_mow) % MINUTES_PER_WEEK        # how far into it
    if span == 0:
        return False, 0
    if offset < span:
        return True, (close_mow - now_mow) % MINUTES_PER_WEEK
    return False, 0


def evaluate(cfg_hours: dict[str, Any] | None, now_utc) -> dict[str, Any]:
    """Evaluate the schedule at ``now_utc`` (a timezone-naive UTC datetime).

    Returns {enabled, open, seconds_to_close, seconds_to_open}. When the gate is
    disabled or unconfigured, ``enabled`` is False and callers must treat the
    market as always tradable.
    """
    cfg_hours = cfg_hours or {}
    if not cfg_hours.get("enabled"):
        return {"enabled": False, "open": True,
                "seconds_to_close": None, "seconds_to_open": None}
    windows = _windows(cfg_hours)
    if not windows:
        # Enabled but no parsable windows -> FAIL SAFE: treat as closed so we do
        # not silently trade an unguarded schedule.
        return {"enabled": True, "open": False,
                "seconds_to_close": 0, "seconds_to_open": None}
    now_mow = _now_mow(now_utc)
    best_to_close: int | None = None
    to_open_candidates: list[int] = []
    for open_mow, close_mow in windows:
        inside, to_close = _contains(open_mow, close_mow, now_mow)
        if inside:
            if best_to_close is None or to_close < best_to_close:
                best_to_close = to_close
        else:
            to_open_candidates.append((open_mow - now_mow) % MINUTES_PER_WEEK)
    if best_to_close is not None:
        return {"enabled": True, "open": True,
                "seconds_to_close": int(best_to_close) * 60, "seconds_to_open": None}
    to_open = min(to_open_candidates) if to_open_candidates else None
    return {"enabled": True, "open": False, "seconds_to_close": 0,
            "seconds_to_open": int(to_open) * 60 if to_open is not None else None}
