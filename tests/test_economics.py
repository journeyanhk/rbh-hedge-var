from decimal import Decimal

from rbh_hedge_var import economics as ec


def test_executable_vwap_walks_book():
    # buy 3 units: 1@100, 2@101 -> (100+202)/3
    asks = [(Decimal("100"), Decimal("1")), (Decimal("101"), Decimal("5"))]
    vwap = ec.executable_vwap(asks, Decimal("3"))
    assert vwap == (Decimal("100") + Decimal("101") * 2) / Decimal("3")


def test_executable_vwap_insufficient_depth_returns_none():
    asks = [(Decimal("100"), Decimal("1"))]
    assert ec.executable_vwap(asks, Decimal("5")) is None


def test_net_hourly_funding_uses_abs_spread():
    net = ec.net_hourly_funding_usdt(
        "short_var_long_lighter", Decimal("12000"),
        Decimal("0.000142"), Decimal("0.000013"),
    )
    assert net == abs(Decimal("0.000142") - Decimal("0.000013")) * Decimal("12000")


def test_net_hourly_none_when_rate_unknown():
    assert ec.net_hourly_funding_usdt("x", Decimal("12000"), None, Decimal("0.0001")) is None


def test_break_even_hours_formula():
    # cost 18, basis gain 11 -> net 7; net_hourly 1.5/h -> 7/1.5
    be = ec.break_even_hours(Decimal("18"), Decimal("11"), Decimal("1.5"))
    assert be == Decimal("7") / Decimal("1.5")


def test_break_even_zero_when_basis_covers_cost():
    assert ec.break_even_hours(Decimal("5"), Decimal("9"), Decimal("1.5")) == Decimal("0")


def test_break_even_none_when_no_edge():
    assert ec.break_even_hours(Decimal("5"), Decimal("0"), Decimal("0")) is None
    assert ec.break_even_hours(Decimal("5"), Decimal("0"), None) is None


def test_entry_basis_gain_only_when_favourable():
    # short var / long lighter benefits from positive basis (var richer)
    g = ec.entry_basis_gain_usdt("short_var_long_lighter", Decimal("12000"), Decimal("0.001"))
    assert g == Decimal("0.001") * Decimal("12000")
    # negative basis is not a gain for this direction
    assert ec.entry_basis_gain_usdt("short_var_long_lighter", Decimal("12000"), Decimal("-0.001")) == Decimal("0")
