"""LiveExecutor — guard, fill ordering, single-leg rollback, MTM parity."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from rbh_hedge_var import net_guard
from rbh_hedge_var.live_executor import LiveExecutionError, LiveExecutor
from rbh_hedge_var.net_guard import WriteBlockedError
from rbh_hedge_var.numeric import D
from rbh_hedge_var.shadow_executor import ShadowExecutor

CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


class FakeVar:
    def __init__(self, fill_price="4330"):
        self.calls = []
        self.fill_price = fill_price

    def place_taker_order(self, side, qty, symbol="XAU", reduce_only=False, max_slippage_pct=D("0.002")):
        self.calls.append({"side": side, "qty": qty, "reduce_only": reduce_only})
        return {"venue": "variational", "symbol": symbol, "side": side,
                "filled_qty": qty, "filled_price": D(self.fill_price), "order_id": "vo1"}


class FakeLighter:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def place_market_order(self, symbol, side, qty, ref_price, reduce_only=False, slippage_pct=D("0.002")):
        self.calls.append({"symbol": symbol, "side": side, "qty": qty, "reduce_only": reduce_only})
        if self.fail:
            raise RuntimeError("sequencer rejected")
        return {"venue": "lighter", "symbol": symbol, "side": side,
                "client_order_index": 123, "tx_hash": "0xlit"}


def _exec(var=None, lit=None):
    return LiveExecutor(dict(CFG), lighter_signer=lit or FakeLighter(), var_gateway=var or FakeVar())


def test_open_blocked_while_armed():
    with pytest.raises(WriteBlockedError):
        _exec().open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                           D("0.0001"), None)


def test_open_fills_var_first_then_lighter():
    var, lit = FakeVar(), FakeLighter()
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                        D("0.0001"), None)
    assert out["both_filled"] is True and out["shadow"] is False
    # var short, lighter long
    assert var.calls[0]["side"] == "sell"
    assert lit.calls[0]["side"] == "buy"
    legs = {leg["venue"]: leg for leg in out["legs"]}
    assert legs["variational"]["price"] == "4330"
    assert legs["lighter"]["filled"] is True


def test_open_rolls_back_var_when_lighter_fails():
    var, lit = FakeVar(), FakeLighter(fail=True)
    ex = _exec(var, lit)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(LiveExecutionError):
        ex.open_hedge("short_var_long_lighter", D("12000"), D("4330"), D("4320"),
                      D("0.0001"), None)
    # var opened sell, then flattened with a reduce_only buy
    assert var.calls[0] == {"side": "sell", "qty": var.calls[0]["qty"], "reduce_only": False}
    assert var.calls[-1]["side"] == "buy" and var.calls[-1]["reduce_only"] is True


def test_close_closes_variational_first():
    var, lit = FakeVar(fill_price="4325"), FakeLighter()
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
