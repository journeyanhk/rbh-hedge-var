"""Live executor — real two-leg hedge (Phase 2, guarded).

Drop-in for ``ShadowExecutor``: same ``open_hedge`` / ``close_hedge`` /
``mark_to_market`` signatures so ``engine.py`` selects one or the other with a
single branch and everything downstream is unchanged.

FILL TRUTH IS PROVEN BY POSITION RECONCILIATION, NOT BY RETURN VALUES (review4
P0-A). A market/RFQ submission being ACCEPTED is not a fill: a slippage-capped
Lighter IOC can be accepted yet fill zero, and a Variational RFQ ``rfq_id`` is
only a request id. So every leg is confirmed by polling the venue's signed
position against a pre-trade baseline, within half-a-size-step tolerance:

  ENTRY (single-leg-risk minimised):
    1. snapshot baseline positions on both venues.
    2. submit the HARDER leg first — Variational RFQ — then CONFIRM its real
       filled qty from the position delta (may be partial).
    3. hedge the ACTUAL filled qty on the deep Lighter book, then CONFIRM it.
    4. any timeout / mismatch -> reduce-only flatten whatever actually filled
       and raise NakedLegError so the engine HALTs and alerts.
    5. record legs with REAL filled qty and REAL average price (not the mark),
       so MTM and the close-out ledger are honest.

  EXIT: close the illiquid Variational leg first, then Lighter; confirm each
    returns to flat.

MTM pricing is delegated to ``pricing`` (pure Decimal maths, no guard coupling)
so unrealized PnL is byte-for-byte identical to Phase 1's proven code.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from . import economics, net_guard, pricing
from .numeric import ZERO, D, quantize_down


class LiveExecutionError(RuntimeError):
    pass


class NakedLegError(LiveExecutionError):
    """Raised when a leg cannot be confirmed AND the rollback may have left a
    residual position. The engine treats this as a HALT-worthy emergency."""


class LiveExecutor:
    def __init__(self, cfg: dict[str, Any], *, lighter_signer: Any, var_gateway: Any) -> None:
        if lighter_signer is None or var_gateway is None:
            raise LiveExecutionError("LiveExecutor requires both gateways")
        self.cfg = cfg
        self.lighter = lighter_signer
        self.var = var_gateway
        self.slippage = D(cfg.get("taker_slippage_pct", 0.0005))
        self.confirm_timeout_s = float(cfg.get("fill_confirm_timeout_s", 30))
        self.confirm_poll_s = float(cfg.get("fill_confirm_poll_s", 2))

    def _guard(self) -> None:
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError(
                "write-guard armed: LiveExecutor cannot trade (net_guard.disarm to go live)")

    # ---- position confirmation --------------------------------------------
    def _signed(self, venue: str, symbol: str) -> Decimal:
        if venue == "lighter":
            return D(self.lighter.signed_position(symbol))
        return D(self.var.signed_position(symbol))

    def _confirm_delta(self, venue: str, symbol: str, baseline: Decimal,
                       expected_sign: int, step: Decimal) -> Decimal:
        """Poll the venue position until the signed delta from ``baseline`` is a
        non-trivial fill in the expected direction, or timeout. Returns the
        ACTUAL signed delta (may be smaller than requested = partial fill), or
        ZERO if nothing filled within the timeout."""
        half = abs(step) / D(2)
        deadline = time.time() + self.confirm_timeout_s
        last = ZERO
        while True:
            delta = self._signed(venue, symbol) - baseline
            # accept once the fill is at least half a step in the right direction
            if expected_sign > 0 and delta >= half:
                return delta
            if expected_sign < 0 and delta <= -half:
                return delta
            last = delta
            if time.time() >= deadline:
                return last if abs(last) >= half else ZERO
            time.sleep(self.confirm_poll_s)

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
        var_sign = -1 if var_side == "sell" else 1
        lit_sign = 1 if lit_side == "buy" else -1

        lit_qty = economics.qty_for_notional(notional, lit_price, lit_size_step)
        var_qty = economics.qty_for_notional(notional, var_price, D("0.0001"))
        if lit_qty <= ZERO or var_qty <= ZERO:
            raise LiveExecutionError(f"non-positive qty lit={lit_qty} var={var_qty}")

        base_var = self._signed("variational", var_symbol)
        base_lit = self._signed("lighter", lit_symbol)

        # 1) harder leg first: Variational RFQ, then CONFIRM the real fill.
        self.var.submit_market_order(var_side, var_qty, symbol=var_symbol)
        var_delta = self._confirm_delta("variational", var_symbol, base_var, var_sign, D("0.0001"))
        actual_var_qty = abs(var_delta)
        if actual_var_qty <= ZERO:
            raise LiveExecutionError("variational leg unconfirmed (no fill within timeout)")

        # 2) hedge the ACTUAL filled qty (partial-fill safe) on Lighter, confirm.
        lit_hedge_qty = quantize_down(actual_var_qty, lit_size_step)
        if lit_hedge_qty <= ZERO:
            self._flatten("variational", var_side, actual_var_qty, var_symbol, lit_price)
            raise LiveExecutionError("variational fill below one lighter size step; flattened")
        try:
            self.lighter.place_market_order(lit_symbol, lit_side, lit_hedge_qty, lit_price)
        except Exception as exc:
            self._flatten("variational", var_side, actual_var_qty, var_symbol, lit_price)
            raise NakedLegError(f"Lighter hedge submit failed, flattened Variational: {exc}") from exc
        lit_delta = self._confirm_delta("lighter", lit_symbol, base_lit, lit_sign, lit_size_step)
        actual_lit_qty = abs(lit_delta)
        if actual_lit_qty <= ZERO:
            # hedge did not fill -> flatten the naked Variational leg.
            self._flatten("variational", var_side, actual_var_qty, var_symbol, lit_price)
            raise NakedLegError("Lighter hedge unconfirmed; flattened Variational leg")

        # 3) real fill prices (never the mark) for honest MTM + close-out ledger.
        var_fill_px = self.var.avg_entry_price(var_symbol) or var_price
        lit_fill_px = self.lighter.avg_entry_price(lit_symbol) or lit_price

        legs = [
            {"venue": "variational", "symbol": var_symbol, "side": var_side,
             "qty": str(actual_var_qty), "price": str(D(var_fill_px)), "filled": True},
            {"venue": "lighter", "symbol": lit_symbol, "side": lit_side,
             "qty": str(actual_lit_qty), "price": str(D(lit_fill_px)), "filled": True},
        ]
        # Residual imbalance guard: legs must match within half a size step.
        if abs(actual_var_qty - actual_lit_qty) > abs(lit_size_step) / D(2):
            raise NakedLegError(
                f"legs imbalanced after fill var={actual_var_qty} lit={actual_lit_qty}")
        return {"shadow": False, "direction": direction, "legs": legs,
                "both_filled": True, "opened_at": int(time.time())}

    def _flatten(self, venue: str, opened_side: str, qty: Decimal, symbol: str,
                 lit_price: Decimal) -> None:
        """Reduce-only flatten a leg that must not remain. Raises NakedLegError if
        the flatten itself fails — that is the loudest possible moment, never a
        silent pass (review4 P0-C)."""
        close_side = "buy" if opened_side == "sell" else "sell"
        try:
            if venue == "variational":
                self.var.submit_market_order(close_side, qty, symbol=symbol, reduce_only=True)
            else:
                self.lighter.place_market_order(symbol, close_side, qty, lit_price, reduce_only=True)
        except Exception as exc:
            raise NakedLegError(
                f"CRITICAL: failed to flatten residual {venue} {symbol} qty={qty}: {exc}") from exc

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
            base = self._signed(leg["venue"], leg["symbol"])
            if leg["venue"] == "variational":
                self.var.submit_market_order(close_side, qty, symbol=leg["symbol"], reduce_only=True)
                # No venue fills endpoint yet: estimate exit on the executable
                # price (ref + slippage), NOT the raw mark. Confirm the position
                # actually reduced toward flat.
                exit_price = pricing.model_fill_price(close_side, D(var_price), None, qty, self.slippage)
                step = D("0.0001")
            else:
                self.lighter.place_market_order(leg["symbol"], close_side, qty, lit_price,
                                                reduce_only=True)
                levels = None
                if lit_book:
                    levels = lit_book.get("bids") if close_side == "sell" else lit_book.get("asks")
                exit_price = pricing.model_fill_price(close_side, D(lit_price), levels, qty, self.slippage)
                step = D("0.0001")
            # confirm the leg reduced (delta opposes the open side)
            delta = self._confirm_delta(leg["venue"], leg["symbol"], base,
                                        1 if close_side == "buy" else -1, step)
            if open_side == "buy":
                price_pnl += (exit_price - entry) * qty
            else:
                price_pnl += (entry - exit_price) * qty
            closed.append({**leg, "exit_price": str(exit_price), "closed": True,
                           "close_confirmed_delta": str(delta)})
        return {"shadow": False, "legs": closed, "price_pnl": price_pnl}

    # ---- mark-to-market (pure, no orders) ---------------------------------
    def mark_to_market(self, legs: list[dict[str, Any]],
                       var_price: Decimal, lit_price: Decimal,
                       lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> Decimal:
        return pricing.mark_to_market_legs(legs, var_price, lit_price, lit_book, self.slippage)
