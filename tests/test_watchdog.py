from decimal import Decimal

from rbh_hedge_var import watchdog as wd
from rbh_hedge_var.watchdog import (
    ACTION_NONE, ACTION_FLATTEN_SINGLE_LEG, ACTION_HALT_DRAWDOWN,
)

LEGS = [
    {"venue": "variational", "side": "sell", "qty": "2"},
    {"venue": "lighter", "side": "buy", "qty": "2"},
]


def test_balanced_ok():
    live = {"variational": Decimal("-2"), "lighter": Decimal("2")}
    assert wd.check_single_leg(LEGS, live).action == ACTION_NONE


def test_single_leg_flagged():
    live = {"variational": Decimal("0"), "lighter": Decimal("2")}
    v = wd.check_single_leg(LEGS, live)
    assert v.action == ACTION_FLATTEN_SINGLE_LEG


def test_shadow_no_live_positions_ok():
    assert wd.check_single_leg(LEGS, None).action == ACTION_NONE


def test_drawdown_breach():
    v = wd.check_drawdown(Decimal("-16"), Decimal("15"))
    assert v.action == ACTION_HALT_DRAWDOWN


def test_drawdown_within_limit():
    assert wd.check_drawdown(Decimal("-5"), Decimal("15")).action == ACTION_NONE
