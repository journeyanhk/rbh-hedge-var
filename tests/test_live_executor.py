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
# make the confirmation loop instant in tests
CFG = {**CFG, "fill_confirm_timeout_s": 1, "fill_confirm_poll_s": 0}


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
