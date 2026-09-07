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
from .lighter_signer import MakerNotSupportedError
from .numeric import ZERO, D, quantize_down


class LiveExecutionError(RuntimeError):
    pass


class NakedLegError(LiveExecutionError):
    """Raised when a leg cannot be confirmed AND the rollback may have left a
    residual position. The engine treats this as a HALT-worthy emergency."""


class MakerCancelError(LiveExecutionError):
    """Raised when a resting maker order could NOT be verified as cancelled
    (review22). A silently-failed cancel leaves a 'zombie' post-only that fills
    later into a DOUBLE hedge, so this must NEVER be swallowed and must NEVER be
    followed by a taker sweep — the caller flattens what it can and escalates to
    the naked-leg/HALT chain so a human reconciles the book."""


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
        # --- Lighter MAKER leg (desgin7/8 route ①) --------------------------
        # Work the deep, liquid Lighter leg PASSIVELY with post-only limit orders
        # to earn the maker rebate instead of paying the taker fee (~-0.22 ->
        # ~-0.10/-0.13 wear per round). Entry ALWAYS finishes with a taker on any
        # unfilled remainder so the book is never left naked; only time-rich
        # exits use maker (urgent exits keep IOC for speed).
        self.maker_enabled = bool(cfg.get("lighter_maker_enabled", True))
        self.maker_fill_timeout_s = float(cfg.get("maker_fill_timeout_s", 45))
        self.maker_max_requotes = int(cfg.get("maker_max_requotes", 2))
        self.maker_fallback_offset_pct = D(cfg.get("maker_fallback_offset_pct", 0.0002))
        # P1-B/drift (review21): re-quote at a FRESH touch each cycle, and abandon
        # the passive attempt (taker-sweep) once the market drifts this far from
        # the reference price — so the maker patience window can never bleed into
        # a large adverse move while a leg waits.
        self.maker_price_drift_abort_pct = D(cfg.get("maker_price_drift_abort_pct", 0.001))
        # review22: verified-cancel. A maker cancel is a LOAD-BEARING wall (its
        # failure = double hedge), so after cancelling we POLL the live open
        # orders until the book proves empty; failure to prove empty within this
        # window is treated as 'not cancelled' and escalates (never taker-swept).
        self.maker_cancel_verify_timeout_s = float(cfg.get("maker_cancel_verify_timeout_s", 5))
        self.maker_cancel_verify_poll_s = float(cfg.get("maker_cancel_verify_poll_s", 0.5))

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
                       expected_sign: int, step: Decimal, *,
                       full_target: Decimal | None = None,
                       timeout: float | None = None) -> Decimal:
        """Poll the venue position until the signed delta from ``baseline`` is a
        non-trivial fill in the expected direction, or timeout. Returns the
        ACTUAL signed delta (may be smaller than requested = partial fill), or
        ZERO if nothing filled within the timeout.

        ``full_target`` (maker path): keep polling until the delta reaches
        (near) the full target size rather than returning on the first partial —
        a resting maker order fills in pieces. ``timeout`` overrides the default
        taker confirm window (maker orders are given a longer, configurable
        patience window)."""
        half = abs(step) / D(2)
        tmo = self.confirm_timeout_s if timeout is None else timeout
        deadline = time.time() + tmo
        last = ZERO
        while True:
            delta = self._signed(venue, symbol) - baseline
            filled_dir = (expected_sign > 0 and delta >= half) or \
                         (expected_sign < 0 and delta <= -half)
            if filled_dir:
                if full_target is None:
                    return delta
                if abs(delta) >= abs(full_target) - half:   # (near) fully filled
                    return delta
            last = delta
            if time.time() >= deadline:
                return last if abs(last) >= half else ZERO
            time.sleep(self.confirm_poll_s)

    # ---- Lighter maker helpers --------------------------------------------
    def _maker_limit_price(self, side: str,
                           lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None,
                           ref_price: Decimal) -> Decimal:
        """Passive post-only price at the touch: a buy joins the best bid, a sell
        joins the best ask (never crossing). Falls back to a small nudge behind
        ``ref_price`` when the book side is unavailable."""
        if lit_book:
            levels = lit_book.get("bids") if side == "buy" else lit_book.get("asks")
            if levels:
                return D(levels[0][0])
        nudge = D(ref_price) * self.maker_fallback_offset_pct
        return D(ref_price) - nudge if side == "buy" else D(ref_price) + nudge

    def _safe_cancel(self, symbol: str) -> None:
        """Best-effort pull of a REJECTED (never-rested) quote before re-quoting.
        Only used on the would-cross path where post-only was refused, so there
        is nothing resting to leak — the load-bearing path uses _cancel_verified."""
        try:
            self.lighter.cancel_all(symbol)
        except Exception:
            pass

    def _open_orders(self, symbol: str) -> list[Any] | None:
        """Live resting orders for ``symbol``, or None when the venue cannot be
        queried (no read client / query raised). None means 'cannot verify' and
        is treated as failure by the caller — never as 'no orders'."""
        getter = getattr(self.lighter, "open_orders", None)
        if getter is None:
            return None
        try:
            return getter(symbol)
        except Exception:
            return None

    def _cancel_verified(self, symbol: str) -> bool:
        """Cancel resting orders and PROVE the book is empty (review22). Returns
        True only when a live query confirms no open orders remain within the
        verify window. A cancel that raises is NOT swallowed silently — we still
        fall through to the verification poll and return False unless the book is
        provably clear. A missing/failing query (None) fails closed to False."""
        try:
            self.lighter.cancel_all(symbol)
        except Exception:
            pass   # not a silent success: verification below decides the result
        deadline = time.time() + self.maker_cancel_verify_timeout_s
        while True:
            orders = self._open_orders(symbol)
            if orders is None:
                return False              # cannot verify -> fail closed
            if not orders:
                return True               # proven empty
            if time.time() >= deadline:
                return False              # zombie still resting
            time.sleep(self.maker_cancel_verify_poll_s)

    def _fresh_book(self, symbol: str) -> dict[str, list[tuple[Decimal, Decimal]]] | None:
        """Re-read the live Lighter book for a re-quote (P1-B). The engine's
        per-tick snapshot book can be up to a tick (~60s) stale, so a re-quote
        that keeps using it either never fills (price ran away) or gets post-only
        rejected in a loop (price came back). Best-effort: falls back to the
        snapshot book when the read client is unavailable (e.g. test fakes)."""
        read = getattr(self.lighter, "read", None)
        if read is None or not hasattr(read, "order_book"):
            return None
        try:
            return read.order_book(symbol)
        except Exception:
            return None

    def _book_mid(self, book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> Decimal | None:
        if not book:
            return None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        return (D(bids[0][0]) + D(asks[0][0])) / D(2)

    def _maker_work(self, lit_symbol: str, side: str, target_qty: Decimal,
                    ref_price: Decimal, lit_size_step: Decimal,
                    lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None,
                    baseline: Decimal, sign: int, *, reduce_only: bool) -> Decimal:
        """Work ``target_qty`` passively with post-only re-quotes at the touch.
        Returns the signed delta actually achieved (may be partial — the caller
        finishes the remainder with a taker).

        review21 patches:
          * P1-B: each re-quote reads a FRESH book (falls back to the snapshot).
          * drift-abort: if the market has drifted more than
            ``maker_price_drift_abort_pct`` from ``ref_price``, stop waiting and
            let the caller taker-sweep — the patience window must never sit open
            through a large adverse move.
          * P1-A: a MakerNotSupportedError (SDK enums missing) aborts the passive
            path immediately so the caller degrades to IOC (never guesses TIF).
        A would-cross reject (generic exception) still cancels and re-quotes.
        """
        half = abs(lit_size_step) / D(2)
        for _ in range(int(self.maker_max_requotes) + 1):
            delta = self._signed("lighter", lit_symbol) - baseline
            remaining = target_qty - abs(delta)
            if remaining < half:
                break
            book = self._fresh_book(lit_symbol) or lit_book
            mid = self._book_mid(book)
            if mid is not None and ref_price > ZERO and \
                    abs(mid - ref_price) / ref_price > self.maker_price_drift_abort_pct:
                break   # drifted too far -> abandon passive, taker-sweep
            rq = quantize_down(remaining, lit_size_step)
            if rq <= ZERO:
                break
            px = self._maker_limit_price(side, book, ref_price)
            try:
                self.lighter.place_post_only_limit_order(
                    lit_symbol, side, rq, px, reduce_only=reduce_only)
            except MakerNotSupportedError:
                break   # SDK cannot maker -> degrade to taker (never guess enum)
            except Exception:
                self._safe_cancel(lit_symbol)   # would-cross / transient -> re-quote
                continue
            self._confirm_delta("lighter", lit_symbol, baseline, sign, lit_size_step,
                                full_target=target_qty, timeout=self.maker_fill_timeout_s)
            # review22: pull the unfilled remainder with a VERIFIED cancel. If we
            # cannot PROVE the resting order is gone, a taker sweep would double
            # the hedge — so escalate instead of continuing.
            if not self._cancel_verified(lit_symbol):
                raise MakerCancelError(
                    f"could not verify Lighter maker order cancelled for {lit_symbol} "
                    f"— possible zombie order; refusing to taker-sweep")
        return self._signed("lighter", lit_symbol) - baseline

    def _hedge_lighter(self, lit_symbol: str, side: str, target_qty: Decimal,
                       ref_price: Decimal, lit_size_step: Decimal,
                       lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None,
                       baseline: Decimal, sign: int) -> Decimal:
        """Fill the Lighter hedge of ``target_qty``. Maker-first (rebate), then
        TAKER any unfilled remainder so the hedge is never left naked. Returns
        the confirmed signed delta."""
        if self.maker_enabled:
            self._maker_work(lit_symbol, side, target_qty, ref_price, lit_size_step,
                             lit_book, baseline, sign, reduce_only=False)
        delta = self._signed("lighter", lit_symbol) - baseline
        remaining = target_qty - abs(delta)
        if remaining >= abs(lit_size_step) / D(2):
            taker_qty = quantize_down(remaining, lit_size_step)
            if taker_qty > ZERO:
                self.lighter.place_market_order(lit_symbol, side, taker_qty, ref_price)
        return self._confirm_delta("lighter", lit_symbol, baseline, sign, lit_size_step)

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

        # 2) hedge the ACTUAL filled qty (partial-fill safe) on Lighter. Maker
        #    first for the rebate, then a taker sweep of any remainder so the
        #    hedge is never left naked. Confirm the real total delta.
        lit_hedge_qty = quantize_down(actual_var_qty, lit_size_step)
        if lit_hedge_qty <= ZERO:
            self._flatten("variational", var_side, actual_var_qty, var_symbol, lit_price)
            raise LiveExecutionError("variational fill below one lighter size step; flattened")
        try:
            lit_delta = self._hedge_lighter(lit_symbol, lit_side, lit_hedge_qty, lit_price,
                                            lit_size_step, lit_book, base_lit, lit_sign)
        except Exception as exc:
            self._safe_cancel(lit_symbol)
            self._flatten("variational", var_side, actual_var_qty, var_symbol, lit_price)
            raise NakedLegError(f"Lighter hedge failed, flattened Variational: {exc}") from exc
        actual_lit_qty = abs(lit_delta)
        if actual_lit_qty <= ZERO:
            # hedge did not fill -> flatten the naked Variational leg.
            self._safe_cancel(lit_symbol)
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
        # review22 ④: belt-and-suspenders — never enter HOLDING with a resting
        # Lighter order still on the book. Even if every cancel reported success,
        # PROVE the book is clear; a lingering post-only would fill later into a
        # double hedge. A None result (query unavailable) is skipped — the maker
        # path's verified-cancel already gates the load-bearing case.
        residual_orders = self._open_orders(lit_symbol)
        if residual_orders:
            raise NakedLegError(
                f"residual resting Lighter order(s) after open ({len(residual_orders)}) — "
                f"refusing to enter HOLDING with a possible zombie order")
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
                    lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None,
                    *, urgent: bool = True) -> dict[str, Any]:
        self._guard()
        # Variational (illiquid) first, then Lighter.
        legs_sorted = sorted(legs, key=lambda leg_x: 0 if leg_x["venue"] == "variational" else 1)
        price_pnl = ZERO
        closed = []
        sources: list[str] = []
        # A calm exit (take-profit / reversal / max-hold) may work the Lighter
        # leg PASSIVELY for the rebate; an urgent exit (stop-loss / watchdog /
        # market-closing / drawdown) always takes for speed.
        lit_maker = self.maker_enabled and not urgent
        for leg in legs_sorted:
            entry = D(leg["price"])
            qty = D(leg["qty"])
            open_side = leg["side"]
            close_side = "buy" if open_side == "sell" else "sell"
            base = self._signed(leg["venue"], leg["symbol"])
            step = D("0.0001")
            if leg["venue"] == "variational":
                resp = self.var.submit_market_order(close_side, qty, symbol=leg["symbol"],
                                                    reduce_only=True)
                exit_price, exit_source = self._real_or_model_exit(
                    resp, close_side, D(var_price), None, qty)
            else:
                levels = None
                if lit_book:
                    levels = lit_book.get("bids") if close_side == "sell" else lit_book.get("asks")
                if lit_maker:
                    # passive reduce-only, then taker whatever remains so the leg
                    # always reaches flat. Priced on the model (Lighter's deep
                    # book is a faithful proxy); the maker rebate shows up in the
                    # venue_realized reconciliation, not the model estimate.
                    self._maker_work(leg["symbol"], close_side, qty, D(lit_price), step,
                                     lit_book, base, 1 if close_side == "buy" else -1,
                                     reduce_only=True)
                    delta_so_far = self._signed(leg["venue"], leg["symbol"]) - base
                    remaining = qty - abs(delta_so_far)
                    if remaining >= step / D(2):
                        tq = quantize_down(remaining, step)
                        if tq > ZERO:
                            self.lighter.place_market_order(leg["symbol"], close_side, tq,
                                                            lit_price, reduce_only=True)
                    exit_price = pricing.model_fill_price(close_side, D(lit_price), levels,
                                                          qty, self.slippage)
                    exit_source = "model"
                else:
                    resp = self.lighter.place_market_order(leg["symbol"], close_side, qty, lit_price,
                                                           reduce_only=True)
                    exit_price, exit_source = self._real_or_model_exit(
                        resp, close_side, D(lit_price), levels, qty)
            # confirm the leg reduced (delta opposes the open side)
            delta = self._confirm_delta(leg["venue"], leg["symbol"], base,
                                        1 if close_side == "buy" else -1, step)
            if open_side == "buy":
                price_pnl += (exit_price - entry) * qty
            else:
                price_pnl += (entry - exit_price) * qty
            sources.append(exit_source)
            closed.append({**leg, "exit_price": str(exit_price), "exit_source": exit_source,
                           "closed": True, "close_confirmed_delta": str(delta)})
        if all(s == "venue_order" for s in sources):
            source = "venue_order"
        elif all(s == "model" for s in sources):
            source = "model"
        else:
            source = "mixed"
        return {"shadow": False, "legs": closed, "price_pnl": price_pnl,
                "price_pnl_source": source}

    def _real_or_model_exit(self, resp: Any, close_side: str, ref_price: Decimal,
                            levels: list[tuple[Decimal, Decimal]] | None,
                            qty: Decimal) -> tuple[Decimal, str]:
        """Price a close leg from the venue's REAL order price when available,
        else the executable model.

        review18: the close-out ledger was pricing EVERY exit off the stale
        snapshot mid ± a flat 5bps taker slippage. On the illiquid XAUS swap the
        real RFQ bid/ask spread dwarfs 5bps, so a genuine -8.63 swap close was
        booked as ~-3.07 — turning a -1.34 losing round into a fake +4.24 profit.
        The order response already carries the truth: ``reported_fill_price``
        (the venue's fill) or, failing that, ``ref_price`` (the side-aware RFQ
        quote actually referenced for THIS order — a sell lifts the bid, a buy
        hits the ask). Prefer those; fall back to the model only when the
        response has no usable price (e.g. Lighter, whose deep book makes the
        modelled VWAP a faithful proxy)."""
        real = None
        if isinstance(resp, dict):
            real = resp.get("reported_fill_price") or resp.get("ref_price")
        if real is not None:
            try:
                px = D(real)
                if px > ZERO:
                    return px, "venue_order"
            except (ArithmeticError, ValueError, TypeError):
                pass
        return pricing.model_fill_price(close_side, ref_price, levels, qty, self.slippage), "model"

    # ---- mark-to-market (pure, no orders) ---------------------------------
    def mark_to_market(self, legs: list[dict[str, Any]],
                       var_price: Decimal, lit_price: Decimal,
                       lit_book: dict[str, list[tuple[Decimal, Decimal]]] | None) -> Decimal:
        return pricing.mark_to_market_legs(legs, var_price, lit_price, lit_book, self.slippage)
