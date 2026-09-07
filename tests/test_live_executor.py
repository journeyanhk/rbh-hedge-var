"""LiveExecutor — guard, position-confirmed fills, single-leg rollback, MTM parity.

review4 P0-A: fills are proven by polling the venue signed position vs a
pre-trade baseline, NOT by return values. The fakes below model that: an order
moves an internal signed position, so ``_confirm_delta`` observes a real delta.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from rbh_hedge_var import net_guard
from rbh_hedge_var.live_executor import LiveExecutionError, LiveExecutor, NakedLegError
from rbh_hedge_var.net_guard import WriteBlockedError
from rbh_hedge_var.numeric import ZERO, D
from rbh_hedge_var.shadow_executor import ShadowExecutor

CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
# make the confirmation loop instant in tests (incl. the maker patience window)
CFG = {**CFG, "fill_confirm_timeout_s": 1, "fill_confirm_poll_s": 0,
       "lighter_maker_enabled": True,
       "maker_fill_timeout_s": 0, "maker_max_requotes": 2,
       "maker_cancel_verify_timeout_s": 0, "maker_cancel_verify_poll_s": 0}


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


class FakeVar:
    """Position-tracking Variational fake. submit_market_order moves self.pos so
    the executor's reconciliation confirms the fill. ``fill_fraction`` < 1 models
    a partial fill (review4 P0-A partial-fill safety)."""

    def __init__(self, entry="4330", fill_fraction=D("1")):
        self.calls = []
        self.pos = ZERO
        self.entry = D(entry)
        self.fill_fraction = D(fill_fraction)

    def submit_market_order(self, side, qty, *, symbol="XAU", reduce_only=False,
                            max_slippage_pct=D("0.002")):
        filled = D(qty) * self.fill_fraction
        self.calls.append({"side": side, "qty": D(qty), "filled": filled,
                           "reduce_only": reduce_only})
        self.pos += filled if side == "buy" else -filled
        return {"venue": "variational", "symbol": symbol, "side": side,
                "rfq_id": "r1", "status": "accepted", "terminal_ok": False}

    def signed_position(self, symbol="XAU"):
        return self.pos

    def avg_entry_price(self, symbol="XAU"):
        return self.entry


class FakeLighter:
    def __init__(self, fail=False, entry="4320"):
        self.calls = []
        self.pos = ZERO
        self.fail = fail
        self.entry = D(entry)

    def place_market_order(self, symbol, side, qty, ref_price, reduce_only=False,
                           slippage_pct=D("0.002")):
        self.calls.append({"symbol": symbol, "side": side, "qty": D(qty),
                           "reduce_only": reduce_only})
        if self.fail:
            raise RuntimeError("sequencer rejected")
        self.pos += D(qty) if side == "buy" else -D(qty)
        return {"venue": "lighter", "symbol": symbol, "side": side, "tx_hash": "0xlit"}

    def signed_position(self, symbol="XAU"):
        return self.pos

    def avg_entry_price(self, symbol="XAU"):
        return self.entry


def _exec(var=None, lit=None):
    return LiveExecutor(dict(CFG), lighter_signer=lit or FakeLighter(), var_gateway=var or FakeVar())


def test_open_blocked_while_armed():
    with pytest.raises(WriteBlockedError):
        _exec().open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                           D("0.0001"), None)


def test_open_confirms_var_first_then_hedges_lighter():
    var, lit = FakeVar(), FakeLighter()
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), None)
    assert out["both_filled"] is True and out["shadow"] is False
    assert var.calls[0]["side"] == "sell"   # var short first
    assert lit.calls[0]["side"] == "buy"    # lighter long hedge
    legs = {leg["venue"]: leg for leg in out["legs"]}
    # real fill prices come from avg_entry_price, not the mark
    assert legs["variational"]["price"] == "4330"
    assert legs["lighter"]["price"] == "4320"
    assert legs["lighter"]["filled"] is True


def test_open_hedges_actual_partial_var_fill():
    # Variational only fills half -> Lighter must hedge the ACTUAL filled qty.
    var, lit = FakeVar(fill_fraction=D("0.5")), FakeLighter()
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), None)
    legs = {leg["venue"]: leg for leg in out["legs"]}
    # lighter qty matches the actual (partial) variational fill within a step
    assert abs(D(legs["lighter"]["qty"]) - D(legs["variational"]["qty"])) <= D("0.0001")
    assert abs(var.pos) > ZERO and abs(lit.pos) > ZERO


def test_open_rolls_back_var_when_lighter_fails():
    var, lit = FakeVar(), FakeLighter(fail=True)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(NakedLegError):
        ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                      D("0.0001"), None)
    # var opened sell, then flattened with a reduce_only buy
    assert var.calls[0]["side"] == "sell" and var.calls[0]["reduce_only"] is False
    assert var.calls[-1]["side"] == "buy" and var.calls[-1]["reduce_only"] is True
    # rollback returned Variational to flat
    assert abs(var.pos) <= D("0.0001")


def test_open_flatten_failure_screams():
    # Lighter hedge fails AND the Variational flatten also fails -> loud NakedLeg.
    class DeadVar(FakeVar):
        def submit_market_order(self, side, qty, *, symbol="XAU", reduce_only=False,
                                max_slippage_pct=D("0.002")):
            if reduce_only:
                raise RuntimeError("flatten venue down")
            return super().submit_market_order(side, qty, symbol=symbol,
                                                reduce_only=reduce_only)

    ex = _exec(DeadVar(), FakeLighter(fail=True))
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(NakedLegError):
        ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                      D("0.0001"), None)


def test_open_raises_when_var_unconfirmed():
    var, lit = FakeVar(fill_fraction=D("0")), FakeLighter()
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(LiveExecutionError):
        ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                      D("0.0001"), None)
    # never hedged on Lighter since the var leg never confirmed
    assert lit.calls == []


def test_close_closes_variational_first():
    var, lit = FakeVar(entry="4325"), FakeLighter()
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    legs = [
        {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
        {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
    ]
    out = ex.close_hedge(legs, D("4331"), D("4322"), None)
    assert var.calls[0]["reduce_only"] is True
    assert lit.calls[0]["reduce_only"] is True
    assert isinstance(out["price_pnl"], Decimal)


def test_mtm_matches_shadow_and_works_while_armed():
    legs = [
        {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
        {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
    ]
    live = _exec().mark_to_market(legs, D("4331"), D("4322"), None)  # armed: pure, no raise
    shadow = ShadowExecutor(dict(CFG)).mark_to_market(legs, D("4331"), D("4322"), None)
    assert live == shadow


class RefPriceVar(FakeVar):
    """Variational fake whose order response carries the venue's REAL price
    (reported_fill_price), like the live gateway. Models the wide swap spread
    that a stale snapshot mid hides."""

    def __init__(self, entry="4470", real_fill="4406"):
        super().__init__(entry=entry)
        self.real_fill = D(real_fill)

    def submit_market_order(self, side, qty, *, symbol="XAU", reduce_only=False,
                            max_slippage_pct=D("0.002")):
        resp = super().submit_market_order(side, qty, symbol=symbol, reduce_only=reduce_only,
                                           max_slippage_pct=max_slippage_pct)
        resp["reported_fill_price"] = str(self.real_fill)
        return resp


def test_close_books_real_swap_fill_not_stale_mid():
    # review18 incident: on a long-V close the stale snapshot mid (4456) sits far
    # above the real swap fill (4406). The OLD model priced the exit off the mid
    # and booked a fake profit; the fix prices it off the venue's real fill.
    var, lit = RefPriceVar(entry="4470", real_fill="4406"), FakeLighter(entry="4472")
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    legs = [
        {"venue": "variational", "symbol": "XAU", "side": "buy", "qty": "0.1119", "price": "4470"},
        {"venue": "lighter", "symbol": "XAU", "side": "sell", "qty": "0.1119", "price": "4472"},
    ]
    out = ex.close_hedge(legs, D("4456"), D("4407"), None)  # stale optimistic mid
    var_leg = [c for c in out["legs"] if c["venue"] == "variational"][0]
    assert var_leg["exit_source"] == "venue_order"
    assert D(var_leg["exit_price"]) == D("4406")
    # true economics: a small LOSS, not the fake +profit the mid-model produced
    assert out["price_pnl"] < 0
    assert out["price_pnl"] > D("-1")
    assert out["price_pnl_source"] == "mixed"   # var=venue, lit=model


def test_close_falls_back_to_model_without_venue_price():
    # backward-compat: a gateway that returns no real price still books on the
    # model (Lighter's deep book makes that faithful), flagged as such.
    var, lit = FakeVar(entry="4325"), FakeLighter()
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    legs = [
        {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
        {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
    ]
    out = ex.close_hedge(legs, D("4331"), D("4322"), None)
    assert out["price_pnl_source"] == "model"
    assert all(leg["exit_source"] == "model" for leg in out["legs"])


# ---------------------------------------------------------------------------
# Lighter MAKER leg (desgin7/8 route ①)
# ---------------------------------------------------------------------------
_BOOK = {"bids": [(D("4319"), D("100"))], "asks": [(D("4321"), D("100"))]}


class _FakeRead:
    def __init__(self, book):
        self._book = book

    def order_book(self, symbol):
        return self._book


class MakerLighter:
    """Post-only maker fake. A post-only order fills ``maker_fill_fraction`` of
    the requested qty into self.pos (models a passive fill); a taker fills fully.
    Records maker/taker/cancel calls so a test can assert which path ran.

    ``fresh_book`` (review21 P1-B): when given, exposes a ``.read.order_book``
    the executor re-reads on each re-quote — distinct from the stale snapshot
    book passed by the engine — so tests can prove re-quotes use the fresh touch
    and the drift-abort fires."""

    def __init__(self, maker_fill_fraction=D("1"), entry="4320", fresh_book=None,
                 post_only_exc=None, cancel_leaves_zombie=False):
        self.calls = []          # taker
        self.maker_calls = []    # post-only
        self.cancels = []
        self.pos = ZERO
        self.entry = D(entry)
        self.maker_fill_fraction = D(maker_fill_fraction)
        self.post_only_exc = post_only_exc
        self.read = _FakeRead(fresh_book) if fresh_book is not None else None
        # review22: an unfilled maker remainder RESTS on the book until cancelled;
        # cancel_leaves_zombie models the 13:09 bug where a cancel silently failed
        # and left the resting order to fill later into a double hedge.
        self.resting = []
        self.cancel_leaves_zombie = cancel_leaves_zombie

    def place_post_only_limit_order(self, symbol, side, qty, limit_price, reduce_only=False):
        if self.post_only_exc is not None:
            raise self.post_only_exc
        filled = D(qty) * self.maker_fill_fraction
        unfilled = D(qty) - filled
        self.maker_calls.append({"symbol": symbol, "side": side, "qty": D(qty),
                                 "price": D(limit_price), "reduce_only": reduce_only})
        self.pos += filled if side == "buy" else -filled
        if unfilled > ZERO:
            self.resting.append({"symbol": symbol, "side": side,
                                 "remaining_base_amount": unfilled})
        return {"post_only": True, "order_index": 1, "tx_hash": "0xpo"}

    def place_market_order(self, symbol, side, qty, ref_price, reduce_only=False,
                           slippage_pct=D("0.002")):
        self.calls.append({"symbol": symbol, "side": side, "qty": D(qty),
                           "reduce_only": reduce_only})
        self.pos += D(qty) if side == "buy" else -D(qty)
        return {"venue": "lighter", "symbol": symbol, "side": side, "tx_hash": "0xlit"}

    def cancel_all(self, symbol=None):
        self.cancels.append(symbol)
        if not self.cancel_leaves_zombie:
            self.resting = [o for o in self.resting if symbol and o["symbol"] != symbol]
        return {"cancelled": True}

    def open_orders(self, symbol):
        return [o for o in self.resting if o["symbol"] == symbol]

    def signed_position(self, symbol="XAU"):
        return self.pos

    def avg_entry_price(self, symbol="XAU"):
        return self.entry


def test_open_uses_maker_when_book_present():
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("1"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls and lit.maker_calls[0]["side"] == "buy"
    # buy joins the best bid, never crossing (post-only)
    assert lit.maker_calls[0]["price"] == D("4319")
    assert lit.calls == []                      # no taker needed
    assert abs(var.pos) > ZERO and abs(lit.pos) > ZERO


def test_open_taker_sweeps_when_maker_never_fills():
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("0"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls                      # maker was attempted
    assert lit.calls and lit.calls[0]["side"] == "buy"   # taker swept the hedge
    # legs balanced within a step -> no naked leg
    legs = {leg["venue"]: leg for leg in out["legs"]}
    assert abs(D(legs["lighter"]["qty"]) - D(legs["variational"]["qty"])) <= D("0.0001")


def test_open_maker_partial_then_taker_remainder():
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("0.5"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls and lit.calls        # both paths used
    assert abs(abs(var.pos) - abs(lit.pos)) <= D("0.0001")


def test_maker_disabled_uses_taker_only():
    cfg = {**CFG, "lighter_maker_enabled": False}
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("1"))
    ex = LiveExecutor(cfg, lighter_signer=lit, var_gateway=var)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls == []                # maker never attempted
    assert lit.calls and lit.calls[0]["side"] == "buy"


def test_close_calm_exit_works_lighter_leg_as_maker():
    var, lit = FakeVar(entry="4325"), MakerLighter(maker_fill_fraction=D("1"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    lit.pos = D("2.7")   # long lighter leg to close
    legs = [
        {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
        {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
    ]
    out = ex.close_hedge(legs, D("4331"), D("4322"), _BOOK, urgent=False)
    # lighter close is a post-only SELL reduce-only, no taker needed
    assert lit.maker_calls and lit.maker_calls[0]["side"] == "sell"
    assert lit.maker_calls[0]["reduce_only"] is True
    assert lit.calls == []
    lit_leg = [c for c in out["legs"] if c["venue"] == "lighter"][0]
    assert lit_leg["exit_source"] == "model"    # priced on model; rebate via venue_realized


def test_close_urgent_exit_keeps_taker():
    var, lit = FakeVar(entry="4325"), MakerLighter(maker_fill_fraction=D("1"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    lit.pos = D("2.7")
    legs = [
        {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
        {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
    ]
    out = ex.close_hedge(legs, D("4331"), D("4322"), _BOOK, urgent=True)
    assert lit.maker_calls == []                # urgent -> taker only
    assert lit.calls and lit.calls[0]["reduce_only"] is True
    assert isinstance(out["price_pnl"], Decimal)


# ---- review21 P1-A / P1-B / drift-abort ------------------------------------
def test_open_degrades_to_taker_when_sdk_lacks_maker_enums():
    # P1-A: place_post_only raises MakerNotSupportedError (SDK enums missing) ->
    # the passive path aborts immediately and the taker sweeps (never guesses TIF).
    from rbh_hedge_var.lighter_signer import MakerNotSupportedError
    var = FakeVar()
    lit = MakerLighter(post_only_exc=MakerNotSupportedError("no enums"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls == []                 # never recorded a maker fill
    assert lit.calls and lit.calls[0]["side"] == "buy"   # taker did the hedge


def test_maker_requote_uses_fresh_book_not_stale_snapshot():
    # P1-B: the engine passes a STALE snapshot book (best bid 4319) but the fresh
    # read shows best bid 4315 — the maker quote must use the fresh touch.
    fresh = {"bids": [(D("4315"), D("100"))], "asks": [(D("4317"), D("100"))]}
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("1"), fresh_book=fresh)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                  D("0.0001"), _BOOK)   # stale book _BOOK bid=4319
    assert lit.maker_calls and lit.maker_calls[0]["price"] == D("4315")   # fresh


def test_maker_aborts_on_price_drift_and_takers():
    # drift-abort: fresh mid (4401) is >0.1% away from ref (4320) -> abandon the
    # passive attempt and taker-sweep, so the leg never waits through a big move.
    drifted = {"bids": [(D("4400"), D("100"))], "asks": [(D("4402"), D("100"))]}
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("1"), fresh_book=drifted)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls == []                 # drift aborted before any quote
    assert lit.calls and lit.calls[0]["side"] == "buy"   # taker hedged instead


# ---- review22 verified-cancel / zombie order --------------------------------
def test_maker_verified_cancel_clears_book_then_taker_sweeps():
    # a normal cancel provably empties the book, so the taker sweep proceeds and
    # entry succeeds with nothing left resting.
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("0"))
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), _BOOK)
    assert out["both_filled"] is True
    assert lit.maker_calls and lit.calls         # maker attempted, taker swept
    assert lit.cancels                           # cancel was issued
    assert lit.open_orders("XAU") == []          # book proven clear at the end


def test_maker_zombie_cancel_failure_raises_naked_leg_no_double_hedge():
    # review22 CORE: cancel silently fails -> a maker remainder stays RESTING.
    # verified-cancel must refuse to taker-sweep (no double hedge) and the open
    # rolls back the Variational leg with a NakedLegError instead of HOLDING.
    var = FakeVar()
    lit = MakerLighter(maker_fill_fraction=D("0.5"), cancel_leaves_zombie=True)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(NakedLegError):
        ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                      D("0.0001"), _BOOK)
    # no taker sweep happened (would have doubled the hedge)
    assert lit.calls == []
    # Variational leg was flattened back to flat — nothing left naked on Var
    assert var.pos == ZERO


def test_open_residual_resting_order_blocks_holding():
    # review22 ④ belt-and-suspenders: even on the taker path, a lingering resting
    # order must block entry into HOLDING (it could fill later into a double hedge).
    cfg = {**CFG, "lighter_maker_enabled": False}
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("1"))
    lit.resting.append({"symbol": "XAU", "side": "buy",
                        "remaining_base_amount": D("0.5")})
    ex = LiveExecutor(cfg, lighter_signer=lit, var_gateway=var)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(NakedLegError):
        ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                      D("0.0001"), _BOOK)


# ---- var-desgin9 maker cancel self-check (pre-flight gate) -------------------
def test_maker_selfcheck_passes_when_cancel_verifies():
    fresh = {"bids": [(D("4390"), D("100"))], "asks": [(D("4392"), D("100"))]}
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("0"), fresh_book=fresh)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    ok, detail = ex.maker_cancel_selfcheck("XAU", D("0.0001"))
    assert ok is True
    assert lit.pos == ZERO                 # probe never moved the position
    assert lit.open_orders("XAU") == []    # probe order cleaned up


def test_maker_selfcheck_fails_on_zombie_cancel():
    fresh = {"bids": [(D("4390"), D("100"))], "asks": [(D("4392"), D("100"))]}
    var, lit = FakeVar(), MakerLighter(maker_fill_fraction=D("0"), fresh_book=fresh,
                                       cancel_leaves_zombie=True)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    ok, detail = ex.maker_cancel_selfcheck("XAU", D("0.0001"))
    assert ok is False                     # cancel could not be proven clean
