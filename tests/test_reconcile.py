"""Position reconciliation — signed merge, fail-closed, balance check."""
import pytest

from rbh_hedge_var.numeric import D
from rbh_hedge_var.reconcile import ReconcileError, positions_balanced, reconcile_positions


class FakeRead:
    def __init__(self, positions=None, raise_exc=None):
        self._positions = positions or []
        self._raise = raise_exc

    def account_snapshot(self):
        if self._raise:
            raise self._raise
        return {"positions": self._positions}


class FakeVarGw:
    def __init__(self, qty=D("0"), raise_exc=None):
        self._qty = qty
        self._raise = raise_exc
        self.seen_symbol = None

    def signed_position(self, symbol):
        self.seen_symbol = symbol
        if self._raise:
            raise self._raise
        return self._qty


def test_reconcile_returns_signed_positions():
    read = FakeRead(positions=[{"symbol": "XAU", "qty": D("2.7")}])
    live = reconcile_positions("XAU", lighter_read=read, var_gateway=FakeVarGw(D("-2.7")))
    assert live == {"lighter": D("2.7"), "variational": D("-2.7")}


def test_reconcile_reads_each_venue_with_its_own_symbol():
    # var-desgin5 §2: Lighter trades XAU, Variational trades the XAUS swap. Each
    # leg must be read with its own symbol or the var position reads as 0.
    read = FakeRead(positions=[
        {"symbol": "XAU", "qty": D("2.7")},
        {"symbol": "XAUS", "qty": D("99")},   # must NOT be counted on the lighter side
    ])
    gw = FakeVarGw(D("-2.7"))
    live = reconcile_positions("XAU", lighter_read=read, var_gateway=gw, var_symbol="XAUS")
    assert live == {"lighter": D("2.7"), "variational": D("-2.7")}
    assert gw.seen_symbol == "XAUS"


def test_reconcile_raises_when_lighter_read_fails():
    with pytest.raises(ReconcileError):
        reconcile_positions("XAU", lighter_read=FakeRead(raise_exc=RuntimeError("down")),
                            var_gateway=FakeVarGw(D("0")))


def test_reconcile_raises_when_variational_read_fails():
    with pytest.raises(ReconcileError):
        reconcile_positions("XAU", lighter_read=FakeRead(positions=[]),
                            var_gateway=FakeVarGw(raise_exc=RuntimeError("401")))


def test_positions_balanced_true_when_matched():
    legs = [
        {"venue": "variational", "side": "sell", "qty": "2.7"},
        {"venue": "lighter", "side": "buy", "qty": "2.7"},
    ]
    live = {"variational": D("-2.7"), "lighter": D("2.7")}
    assert positions_balanced(legs, live) is True


def test_positions_balanced_false_on_single_leg():
    legs = [
        {"venue": "variational", "side": "sell", "qty": "2.7"},
        {"venue": "lighter", "side": "buy", "qty": "2.7"},
    ]
    live = {"variational": D("-2.7"), "lighter": D("0")}  # lighter leg missing
    assert positions_balanced(legs, live) is False
