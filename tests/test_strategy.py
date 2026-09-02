from decimal import Decimal

from rbh_hedge_var import strategy


def test_choose_direction_auto():
    assert strategy.choose_direction(Decimal("0.0001")) == "short_var_long_lighter"
    assert strategy.choose_direction(Decimal("-0.0001")) == "short_lighter_long_var"
    assert strategy.choose_direction(Decimal("0")) is None
    assert strategy.choose_direction(None) is None


def _snap(**kw):
    base = {
        "spread_hourly": Decimal("0.0001"),
        "var_price": Decimal("4327"),
        "lighter_price": Decimal("4323"),
        "price_diff_abs": Decimal("4"),
        "basis": Decimal("0.0009"),
        "lighter_status": "active",
        "lighter_reduce_only": False,
    }
    base.update(kw)
    return base


CFG = {
    "direction_mode": "auto",
    "entry_spread_threshold_hourly": 0.00002,
    "max_entry_price_diff_usdt": 6.0,
    "max_basis_pct": 0.015,
    "force_exit_basis_pct": 0.02,
    "exit_on_spread_reversal": True,
    "spread_reversal_confirm_ticks": 3,
}


def test_entry_signal_positive_spread_shorts_variational():
    ok, direction, _ = strategy.entry_signal(_snap(), CFG)
    assert ok and direction == "short_var_long_lighter"


def test_entry_blocked_when_spread_unknown():
    ok, _, reason = strategy.entry_signal(_snap(spread_hourly=None), CFG)
    assert not ok and "funding_unit_unknown" in reason


def test_entry_blocked_on_wide_basis():
    ok, _, reason = strategy.entry_signal(_snap(basis=Decimal("0.05")), CFG)
    assert not ok and "basis_too_wide" in reason


def test_exit_on_basis_force():
    snap = _snap(basis=Decimal("0.03"))
    should, reason = strategy.exit_signal(snap, "short_var_long_lighter", CFG, 0)
    assert should and "basis_force_exit" in reason


def test_exit_on_confirmed_reversal():
    snap = _snap(spread_hourly=Decimal("-0.00001"))
    # streak now passed directly (includes current tick); need 3 confirms.
    should, reason = strategy.exit_signal(snap, "short_var_long_lighter", CFG, 3)
    assert should and "reversal" in reason


def test_no_exit_below_confirm_threshold():
    snap = _snap(spread_hourly=Decimal("-0.00001"))
    should, _ = strategy.exit_signal(snap, "short_var_long_lighter", CFG, 2)
    assert not should


def test_no_exit_on_single_reversal_tick():
    snap = _snap(spread_hourly=Decimal("-0.00001"))
    should, _ = strategy.exit_signal(snap, "short_var_long_lighter", CFG, 0)
    assert not should


def test_round_stop_loss_triggers_below_limit():
    cfg = {"max_round_loss_usdt": 8.0}
    should, reason = strategy.round_stop_loss_signal(Decimal("-8.01"), cfg)
    assert should and "round_stop_loss" in reason


def test_round_stop_loss_silent_within_limit():
    cfg = {"max_round_loss_usdt": 8.0}
    should, _ = strategy.round_stop_loss_signal(Decimal("-5"), cfg)
    assert not should


def test_round_stop_loss_disabled_when_zero():
    should, _ = strategy.round_stop_loss_signal(Decimal("-100"), {"max_round_loss_usdt": 0})
    assert not should


def test_take_profit_triggers():
    cfg = {"take_profit_total_pnl_usdt": 5.0}
    should, reason = strategy.take_profit_signal(Decimal("5.5"), cfg)
    assert should and "take_profit" in reason
