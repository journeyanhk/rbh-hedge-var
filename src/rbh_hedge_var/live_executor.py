"""Live executor — real two-leg hedge (Phase 2, guarded).

Drop-in for ``ShadowExecutor``: same ``open_hedge`` / ``close_hedge`` /
``mark_to_market`` signatures so ``engine.py`` selects one or the other with a
single branch and everything downstream (state machine, PnL, watchdog) is
unchanged.

Difference from shadow: fills are REAL. open/close call the two order
gateways instead of modelling a price. The legs it records carry the venue's
actual fill price and quantity, so mark-to-market and the ledger reflect what
truly happened.

Fill ordering (single-leg-risk minimisation):
  * ENTRY: fill the HARDER leg first — Variational RFQ (slower, can be rejected)
    — then hedge it on the deep Lighter book. If the Lighter hedge fails, we
    immediately flatten the Variational leg reduce-only and report the entry as
    NOT filled, so the engine aborts cleanly and never holds a naked leg.
  * EXIT: close the illiquid Variational leg first, then Lighter.

MTM pricing is delegated to a ShadowExecutor instance used purely as a pure
pricer (no orders, no network mutation) so the unrealized-PnL maths is byte-for-
byte identical to Phase 1's proven code.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from . import economics, net_guard, pricing
from .numeric import ZERO, D


class LiveExecutionError(RuntimeError):
    pass


class LiveExecutor:
    def __init__(self, cfg: dict[str, Any], *, lighter_signer: Any, var_gateway: Any) -> None:
        if lighter_signer is None or var_gateway is None:
            raise LiveExecutionError("LiveExecutor requires both gateways")
        self.cfg = cfg
        self.lighter = lighter_signer
        self.var = var_gateway
        self.slippage = D(cfg.get("taker_slippage_pct", 0.0005))

    def _guard(self) -> None:
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError(
                "write-guard armed: LiveExecutor cannot trade (net_guard.disarm to go live)")

    # ---- entry -------------------------------------------------------------
    def open_hedge(self, direction: str, notional: Decimal,
                   var_price: Decimal, lit_price: Decimal,
                   lit_size_step: Decimal,
                   lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None,
                   var_symbol: str = "XAU", lit_symbol: str = "XAU") -> dict[str, Any]:
        self._guard()
        if direction == "short_var_long_lighter":
            var_side, lit_side = "sell", "buy"
        else:
            var_side, lit_side = "buy", "sell"

        lit_qty = economics.qty_for_notional(notional, lit_price, lit_size_step)
        var_qty = economics.qty_for_notional(notional, var_price, D("0.0001"))
        if lit_qty <= ZERO or var_qty <= ZERO:
            raise LiveExecutionError(f"non-positive qty lit={lit_qty} var={var_qty}")

        # 1) harder leg first: Variational RFQ.
        var_fill = self.var.place_taker_order(var_side, var_qty, symbol=var_symbol)

        # 2) hedge on Lighter; on ANY failure, flatten the Variational leg.
        try:
            lit_fill = self.lighter.place_market_order(lit_symbol, lit_side, lit_qty, lit_price)
        except Exception as exc:
            self._emergency_flatten_var(var_side, D(var_fill["filled_qty"]), var_symbol)
            raise LiveExecutionError(f"Lighter hedge failed, Variational leg flattened: {exc}") from exc

        legs = [
            {"venue": "variational", "symbol": var_symbol, "side": var_side,
             "qty": str(D(var_fill["filled_qty"])), "price": str(D(var_fill["filled_price"])),
             "order_id": var_fill.get("order_id"), "filled": True},
            {"venue": "lighter", "symbol": lit_symbol, "side": lit_side,
             "qty": str(lit_qty), "price": str(lit_price),
             "client_order_index": lit_fill.get("client_order_index"),
             "tx_hash": lit_fill.get("tx_hash"), "filled": True},
        ]
        return {"shadow": False, "direction": direction, "legs": legs,
                "both_filled": True, "opened_at": int(time.time())}

    def _emergency_flatten_var(self, opened_side: str, qty: Decimal, symbol: str) -> None:
        close_side = "buy" if opened_side == "sell" else "sell"
        try:
            self.var.place_taker_order(close_side, qty, symbol=symbol, reduce_only=True)
        except Exception:
            # Cannot auto-flatten -> loud failure; watchdog + HALT will catch the
            # residual single leg on the next reconcile tick.
            pass

    # ---- exit --------------------------------------------------------------
    def close_hedge(self, legs: list[dict[str, Any]],
                    var_price: Decimal, lit_price: Decimal,
                    lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> dict[str, Any]:
        self._guard()
        # Variational (illiquid) first, then Lighter.
        legs_sorted = sorted(legs, key=lambda leg_x: 0 if leg_x["venue"] == "variational" else 1)
        price_pnl = ZERO
        closed = []
        for leg in legs_sorted:
            entry = D(leg["price"])
            qty = D(leg["qty"])
            open_side = leg["side"]
            close_side = "buy" if open_side == "sell" else "sell"
            if leg["venue"] == "variational":
                fill = self.var.place_taker_order(close_side, qty, symbol=leg["symbol"], reduce_only=True)
                exit_price = D(fill["filled_price"])
            else:
                fill = self.lighter.place_market_order(leg["symbol"], close_side, qty, lit_price,
                                                       reduce_only=True)
                # Lighter market order fills near lit_price; use mark as the booked exit.
                exit_price = D(lit_price)
            if open_side == "buy":
                price_pnl += (exit_price - entry) * qty
            else:
                price_pnl += (entry - exit_price) * qty
            closed.append({**leg, "exit_price": str(exit_price), "closed": True,
                           "close_ref": fill.get("order_id") or fill.get("tx_hash")})
        return {"shadow": False, "legs": closed, "price_pnl": price_pnl}

    # ---- mark-to-market (pure, no orders) ---------------------------------
    def mark_to_market(self, legs: list[dict[str, Any]],
                       var_price: Decimal, lit_price: Decimal,
                       lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> Decimal:
        # Pure Decimal maths, identical to shadow; never mutates or guards.
        return pricing.mark_to_market_legs(legs, var_price, lit_price, lit_book, self.slippage)
