from decimal import Decimal

from rbh_hedge_var import funding_guard as fg


def test_normalize_hourly_4h_vs_1h():
    # A 0.0142% rate quoted over 4h is ~0.00355%/h.
    r = fg.normalize_hourly(Decimal("0.000142"), 14400)
    assert r == Decimal("0.000142") * Decimal(3600) / Decimal(14400)
    # Same number quoted hourly stays as-is.
    assert fg.normalize_hourly(Decimal("0.000142"), 3600) == Decimal("0.000142")


def test_normalize_hourly_unknown_interval_is_none():
    assert fg.normalize_hourly(Decimal("0.0001"), None) is None
    assert fg.normalize_hourly(Decimal("0.0001"), 0) is None


def test_normalize_hourly_annualized_variational():
    # Variational publishes an annualized figure (>0.01). ~0.2566 APR over 4h.
    r = fg.normalize_hourly(Decimal("0.2566"), 14400)
    periods = Decimal(365 * 24 * 3600) / Decimal(14400)
    expected = (Decimal("0.2566") / periods) * Decimal(3600) / Decimal(14400)
    assert r == expected
    # sanity: should be a small hourly number, not 6%/h
    assert abs(r) < Decimal("0.001")


def test_verify_units_verified():
    res = fg.verify_units(3600, 3600, expected_var_s=3600, expected_lighter_s=3600)
    assert res.verified and res.live_allowed


def test_verify_units_missing_fails_closed():
    res = fg.verify_units(None, 3600, expected_var_s=3600, expected_lighter_s=3600)
    assert res.status == "unverified" and not res.live_allowed


def test_verify_units_mismatch_blocks_live():
    res = fg.verify_units(14400, 3600, expected_var_s=3600, expected_lighter_s=3600)
    assert res.status == "mismatch" and not res.live_allowed


def test_unit_hint_conflicts_ignores_zero_rate():
    # review12: a zero funding rate carries no unit info; the magnitude heuristic
    # would wrongly say "per_interval" and warn on an 'annualized' venue.
    assert fg.unit_hint_conflicts(Decimal("0"), "annualized") is False
    assert fg.unit_hint_conflicts(Decimal("0"), "per_interval") is False


def test_unit_hint_conflicts_still_flags_real_mismatch():
    # A large annualized-magnitude rate declared 'per_interval' should still warn.
    assert fg.unit_hint_conflicts(Decimal("0.25"), "per_interval") is True
