from decimal import Decimal

from rbh_hedge_var import strategy


def test_choose_direction_auto():
    assert strategy.choose_direction(Decimal("0.0001")) == "short_var_long_lighter"
    assert strategy.choose_direction(Decimal("-0.0001")) == "short_lighter_long_var"
    assert strategy.choose_direction(Decimal("0")) is None
    assert strategy.choose_direction(None) is None


def test_lighter_rate_normalized_over_8h_basis():
    # review11: RHC quotes an 8h-basis rate but settles hourly. The economics
    # must divide the quoted rate by 8, so quoted 0.000032 -> hourly 0.000004.
    var_asset = {"symbol": "XAU", "price": "4420", "funding_rate": "0",
                 "funding_interval_s": 14400}
    lit_contract = {"symbol": "XAU", "mark_price": "4420", "status": "active"}
    lit_funding = {"rate": "0.000032", "funding_interval_s": None,
                   "official_interval_s": 3600}
    cfg = {
        "expected_variational_funding_interval_s": 14400,
        "expected_lighter_funding_interval_s": 3600,
        "variational": {"funding_unit": "annualized"},
        "lighter": {"funding_unit": "per_interval", "funding_rate_basis_s": 28800},
        "_attested_lighter_interval_s": 3600,
    }
    snap = strategy.market_snapshot(var_asset, lit_contract, lit_funding, cfg)
    assert snap["lighter_funding_hourly"] == Decimal("0.000004")


def test_lighter_rate_without_basis_defaults_to_settlement_interval():
    # Backward-compat: no funding_rate_basis_s -> falls back to the reference
    # (settlement) interval, i.e. the pre-review11 behavior of treating the
    # quoted rate as hourly.
    var_asset = {"symbol": "XAU", "price": "4420", "funding_rate": "0",
                 "funding_interval_s": 14400}
    lit_contract = {"symbol": "XAU", "mark_price": "4420", "status": "active"}
    lit_funding = {"rate": "0.000032", "funding_interval_s": None,
                   "official_interval_s": 3600}
    cfg = {
        "expected_variational_funding_interval_s": 14400,
        "expected_lighter_funding_interval_s": 3600,
        "variational": {"funding_unit": "annualized"},
        "lighter": {"funding_unit": "per_interval"},
    }
    snap = strategy.market_snapshot(var_asset, lit_contract, lit_funding, cfg)
    assert snap["lighter_funding_hourly"] == Decimal("0.000032")


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


def test_entry_blocked_when_basis_gain_below_floor():
    # review12: with require_entry_basis_gain, adverse/insufficient basis blocks.
    cfg = {**CFG, "require_entry_basis_gain": True, "min_entry_basis_gain_usdt": 1.0}
    ok, _, reason = strategy.entry_signal(_snap(entry_basis_gain_usdt=Decimal("0")), cfg)
    assert not ok and "basis_gain_too_small" in reason


def test_entry_allowed_when_basis_gain_meets_floor():
    cfg = {**CFG, "require_entry_basis_gain": True, "min_entry_basis_gain_usdt": 1.0}
    ok, direction, _ = strategy.entry_signal(_snap(entry_basis_gain_usdt=Decimal("1.5")), cfg)
    assert ok and direction == "short_var_long_lighter"


def test_basis_gain_condition_off_by_default():
    # Without the flag the condition is a no-op even if gain is 0/missing.
    ok, _, _ = strategy.entry_signal(_snap(), CFG)
    assert ok


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
