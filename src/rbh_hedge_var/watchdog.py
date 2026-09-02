"""Single-leg exposure watchdog + drawdown circuit breaker.

The most dangerous hedge failure is being left with one naked leg. This module
compares the state machine's expected legs against reality and returns an
emergency action. In Phase 1 "reality" is the shadow state (always balanced),
but the check is wired exactly as Phase 2 needs it: pass live positions from
both venues and it flags a mismatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .numeric import ZERO, D

ACTION_NONE = "none"
ACTION_FLATTEN_SINGLE_LEG = "flatten_single_leg"
ACTION_HALT_DRAWDOWN = "halt_drawdown"


@dataclass(frozen=True)
class WatchdogVerdict:
    action: str
    reason: str
    detail: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.action == ACTION_NONE


def check_single_leg(expected_legs: list[dict[str, Any]],
                     live_positions: dict[str, Decimal] | None,
                     tolerance: Decimal = D("0.0000001")) -> WatchdogVerdict:
    """Detect a naked/unbalanced leg.

    expected_legs: from state.legs (venue, side, qty).
    live_positions: {venue: signed_qty} observed on the exchanges. None in
                    Phase 1 shadow (nothing to reconcile) -> returns OK.
    """
    if not expected_legs:
        return WatchdogVerdict(ACTION_NONE, "no_open_round", {})
    if live_positions is None:
        return WatchdogVerdict(ACTION_NONE, "shadow_no_live_positions", {})

    mismatches = {}
    present = 0
    for leg in expected_legs:
        venue = leg["venue"]
        want = D(leg["qty"]) * (D(1) if leg["side"] == "buy" else D(-1))
        have = D(live_positions.get(venue, ZERO))
        if abs(have) > tolerance:
            present += 1
        if abs(have - want) > tolerance:
            mismatches[venue] = {"want": str(want), "have": str(have)}

    if present == 1:
        return WatchdogVerdict(
            ACTION_FLATTEN_SINGLE_LEG,
            "only one hedge leg is live — flatten immediately (reduce-only) and halt",
            {"mismatches": mismatches},
        )
    if mismatches:
        return WatchdogVerdict(
            ACTION_FLATTEN_SINGLE_LEG,
            "hedge legs out of balance beyond tolerance",
            {"mismatches": mismatches},
        )
    return WatchdogVerdict(ACTION_NONE, "legs_balanced", {})


def check_drawdown(today_pnl: Decimal, max_daily_loss: Decimal) -> WatchdogVerdict:
    if max_daily_loss > ZERO and D(today_pnl) <= -abs(D(max_daily_loss)):
        return WatchdogVerdict(
            ACTION_HALT_DRAWDOWN,
            f"daily loss {today_pnl} breached limit -{max_daily_loss}",
            {"today_pnl": str(today_pnl), "limit": str(max_daily_loss)},
        )
    return WatchdogVerdict(ACTION_NONE, "within_drawdown_limit", {})
