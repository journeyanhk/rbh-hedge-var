"""Shadow executor — models a two-leg hedge WITHOUT ever sending an order.

Key contract borrowed from rbh-hedge-v2: a request being accepted is NOT a
fill. The shadow fill models two phases per leg (accepted -> confirmed) and
exposes ``filled`` only after the confirm step, so downstream state logic is
written against the same truth the live executor will face in Phase 2.

Fills are priced on the executable VWAP walking the real order book (Lighter)
and the indicative price (Variational), plus a configurable taker slippage, so
the simulated PnL is conservative rather than mark-priced.
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from . import economics, net_guard
from .numeric import D, ZERO


class ShadowLeg:
    def __init__(self, venue: str, symbol: str, side: str, qty: Decimal, price: Decimal) -> None:
        self.venue = venue
        self.symbol = symbol
        self.side = side           # "buy" | "sell"
        self.qty = qty
        self.price = price         # modeled fill price (VWAP + slippage)
        self.client_id = f"shadow-{venue}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.accepted = False
        self.filled = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "symbol": self.symbol, "side": self.side,
            "qty": str(self.qty), "price": str(self.price),
            "client_id": self.client_id,
            "accepted": self.accepted, "filled": self.filled,
        }


class ShadowExecutor:
    """Never touches the network. net_guard stays armed the whole time."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.slippage = D(cfg.get("taker_slippage_pct", 0.0005))

    def _model_price(self, side: str, ref_price: Decimal,
                     book: list[tuple[Decimal, Decimal]] | None, qty: Decimal) -> Decimal:
        vwap = None
        if book:
            vwap = economics.executable_vwap(book, qty)
        base = vwap if vwap is not None else ref_price
        # taker crosses the spread: buys pay up, sells receive less
        if side == "buy":
            return base * (D(1) + self.slippage)
        return base * (D(1) - self.slippage)

    def open_hedge(self, direction: str, notional: Decimal,
                   var_price: Decimal, lit_price: Decimal,
                   lit_size_step: Decimal,
                   lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> dict[str, Any]:
        assert net_guard.is_armed(), "shadow executor requires the write-guard armed"

        if direction == "short_var_long_lighter":
            var_side, lit_side = "sell", "buy"
        else:
            var_side, lit_side = "buy", "sell"

        lit_qty = economics.qty_for_notional(notional, lit_price, lit_size_step)
        var_qty = economics.qty_for_notional(notional, var_price, D("0.0001"))

        lit_levels = None
        if lit_book:
            lit_levels = lit_book.get("asks") if lit_side == "buy" else lit_book.get("bids")

        var_fill = self._model_price(var_side, var_price, None, var_qty)
        lit_fill = self._model_price(lit_side, lit_price, lit_levels, lit_qty)

        legs = [
            ShadowLeg("variational", "XAU", var_side, var_qty, var_fill),
            ShadowLeg("lighter", "XAUT", lit_side, lit_qty, lit_fill),
        ]
        # Phase-accurate: accept both, then confirm both. A real executor could
        # fail between these; the state machine handles a single-confirmed leg.
        for leg in legs:
            leg.accepted = True
        for leg in legs:
            leg.filled = True

        return {
            "shadow": True,
            "direction": direction,
            "legs": [leg.to_dict() for leg in legs],
            "both_filled": all(leg.filled for leg in legs),
            "opened_at": int(time.time()),
        }

    def close_hedge(self, legs: list[dict[str, Any]],
                    var_price: Decimal, lit_price: Decimal,
                    lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> dict[str, Any]:
        assert net_guard.is_armed(), "shadow executor requires the write-guard armed"
        # Close the illiquid leg first (Variational RFQ) then Lighter — mirrors
        # the live ordering we will use in Phase 2.
        price_pnl = ZERO
        closed = []
        for leg in legs:
            entry = D(leg["price"])
            qty = D(leg["qty"])
            venue = leg["venue"]
            open_side = leg["side"]
            close_side = "buy" if open_side == "sell" else "sell"
            if venue == "lighter":
                levels = lit_book.get("bids") if close_side == "sell" else lit_book.get("asks") if lit_book else None
                exit_price = self._model_price(close_side, lit_price, levels, qty)
            else:
                exit_price = self._model_price(close_side, var_price, None, qty)
            # long profit if exit>entry; short profit if entry>exit
            if open_side == "buy":
                price_pnl += (exit_price - entry) * qty
            else:
                price_pnl += (entry - exit_price) * qty
            closed.append({**leg, "exit_price": str(exit_price), "closed": True})
        return {"shadow": True, "legs": closed, "price_pnl": price_pnl}
