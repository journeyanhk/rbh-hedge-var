"""Decimal helpers — every economic quantity in this project is Decimal.

Borrowed principle from rbh-hedge-v2: never let float rounding touch money or
size math. Prices, sizes, rates and cashflows are all Decimal; only display
and transport boundaries convert to str/float.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, getcontext
from typing import Any

getcontext().prec = 40

ZERO = Decimal(0)
ONE = Decimal(1)


def D(value: Any) -> Decimal:
    """Coerce anything json-ish into a Decimal without float contamination."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    if isinstance(value, float):
        # Route through str so 0.1 stays 0.1, not 0.1000000000000000055.
        return Decimal(str(value))
    if isinstance(value, (int, str)):
        try:
            return Decimal(str(value).strip() or "0")
        except Exception:
            return ZERO
    return ZERO


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step


def to_int_scaled(value: Decimal, decimals: int, direction: str = "nearest") -> int:
    """Convert a Decimal to the integer representation an exchange expects.

    Mirrors gbw market.js toExchangeInteger: RHC Lighter takes base_amount and
    price as integers scaled by the market's size/price decimals.
    """
    factor = Decimal(10) ** int(decimals)
    scaled = value * factor
    if direction == "down":
        return int(scaled.to_integral_value(rounding=ROUND_DOWN))
    if direction == "up":
        # ceil
        floored = scaled.to_integral_value(rounding=ROUND_DOWN)
        return int(floored if floored == scaled else floored + 1)
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def fmt(value: Any, places: int = 6) -> str:
    d = D(value)
    q = Decimal(10) ** -places
    return str(d.quantize(q, rounding=ROUND_HALF_UP))
