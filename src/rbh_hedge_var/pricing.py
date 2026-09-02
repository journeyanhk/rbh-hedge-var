"""Pure fill-pricing maths shared by the shadow and live executors.

No network, no order side effects, no write-guard coupling — just Decimal
pricing so both executors compute mark-to-market identically. Factored out of
``shadow_executor`` so ``LiveExecutor`` (which runs with the write-guard
DISARMED) can reuse the exact same proven maths without tripping the shadow
executor's armed-guard assertion.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import economics
from .numeric import ZERO, D


def model_fill_price(side: str, ref_price: Decimal,
                     book: list[tuple[Decimal, Decimal]] | None,
                     qty: Decimal, slippage: Decimal) -> Decimal:
    """Executable price: depth-weighted VWAP when a book is given, else ref, with
    taker slippage crossing the spread (buys pay up, sells receive less)."""
    vwap = economics.executable_vwap(book, qty) if book else None
    base = vwap if vwap is not None else ref_price
    if side == "buy":
        return base * (D(1) + slippage)
    return base * (D(1) - slippage)


def mark_to_market_legs(legs: list[dict[str, Any]],
                        var_price: Decimal, lit_price: Decimal,
                        lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None,
                        slippage: Decimal) -> Decimal:
    """Unrealized price-leg PnL if both legs were closed right now."""
    price_pnl = ZERO
    for leg in legs:
        entry = D(leg["price"])
        qty = D(leg["qty"])
        open_side = leg["side"]
        close_side = "buy" if open_side == "sell" else "sell"
        if leg["venue"] == "lighter":
            levels = None
            if lit_book:
                levels = lit_book.get("bids") if close_side == "sell" else lit_book.get("asks")
            exit_price = model_fill_price(close_side, lit_price, levels, qty, slippage)
        else:
            exit_price = model_fill_price(close_side, var_price, None, qty, slippage)
        if open_side == "buy":
            price_pnl += (exit_price - entry) * qty
        else:
            price_pnl += (entry - exit_price) * qty
    return price_pnl
