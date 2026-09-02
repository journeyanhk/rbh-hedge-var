"""Hedge economics — Decimal, borrowed contracts from rbh-hedge-v2.

Two contracts adopted from the rbh review that VO got wrong:
  * Exit is priced on depth-weighted executable VWAP, NOT the mark price. A
    mark-priced exit understates cost on a thin book.
  * Funding projection uses the discrete settlement interval; we never invent
    an hourly carry when the interval is unknown (that check lives in
    funding_guard, here we just consume verified hourly rates).

Everything returns Decimal or None. None means "not computable" and must never
be coerced to zero by callers.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .numeric import ZERO, D


def executable_vwap(levels: list[tuple[Decimal, Decimal]], target_qty: Decimal) -> Decimal | None:
    """Depth-weighted average fill price for target_qty walking the book.

    levels: list of (price, size), already sorted best-first.
    Returns None if the book cannot cover the full quantity.
    """
    if target_qty <= ZERO or not levels:
        return None
    remaining = target_qty
    cost = ZERO
    for price, size in levels:
        if price <= ZERO or size <= ZERO:
            continue
        take = size if size < remaining else remaining
        cost += take * price
        remaining -= take
        if remaining <= ZERO:
            break
    if remaining > ZERO:
        return None  # insufficient depth
    return cost / target_qty


def qty_for_notional(notional: Decimal, price: Decimal, size_step: Decimal) -> Decimal:
    if price <= ZERO:
        return ZERO
    from .numeric import quantize_down
    return quantize_down(D(notional) / D(price), D(size_step) if size_step else Decimal("0.0001"))


def roundtrip_cost_usdt(notional: Decimal, cfg: dict[str, Any]) -> Decimal:
    """Assumed round-trip cost in USDT. MEASURE live before trusting this."""
    pct = D(cfg.get("assumed_roundtrip_cost_pct", 0.0015))
    return D(notional) * pct


def net_hourly_funding_usdt(direction: str, notional: Decimal,
                            var_hourly: Decimal | None, lit_hourly: Decimal | None) -> Decimal | None:
    """Net funding cashflow per hour for the hedge (positive = we earn).

    Short leg receives funding when its rate is positive; long leg pays it.
    We short the higher-funding leg, so net = |spread| * notional.
    """
    if var_hourly is None or lit_hourly is None:
        return None
    spread = var_hourly - lit_hourly
    # Whichever direction we take, the realized edge is |spread| * notional/hr
    # because we always short the higher and long the lower.
    return abs(spread) * D(notional)


def break_even_hours(roundtrip_cost: Decimal, entry_basis_gain: Decimal,
                     net_hourly: Decimal | None) -> Decimal | None:
    """T_min = max(0, roundtrip_cost - entry_basis_gain) / net_hourly.

    entry_basis_gain: USDT captured from entering with basis on our side.
    Returns None if net_hourly is unknown or <= 0 (never breaks even).
    """
    if net_hourly is None or net_hourly <= ZERO:
        return None
    net_cost = roundtrip_cost - entry_basis_gain
    if net_cost <= ZERO:
        return ZERO
    return net_cost / net_hourly


def entry_basis_gain_usdt(direction: str, notional: Decimal, basis: Decimal | None) -> Decimal:
    """USDT captured if we enter with basis in our favour (short high / long low).

    Positive only when the short leg is the richer-priced one; otherwise 0.
    basis = var_price/lighter_price - 1.
    """
    if basis is None:
        return ZERO
    b = D(basis)
    if direction == "short_var_long_lighter" and b > ZERO:
        return b * D(notional)
    if direction == "short_lighter_long_var" and b < ZERO:
        return abs(b) * D(notional)
    return ZERO
