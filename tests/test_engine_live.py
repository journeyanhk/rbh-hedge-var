"""Engine Phase-2 wiring: executor selection + live reconciliation.

Offline: fetch_snapshot is monkeypatched and fake gateways are injected, so no
network or SDK is touched. Guard is re-armed around every test.
"""
import json
from decimal import Decimal
from pathlib import Path

from rbh_hedge_var import net_guard
from rbh_hedge_var import state_machine as SM
from rbh_hedge_var.engine import Engine
from rbh_hedge_var.numeric import D

CFG_BASE = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


def _snap(**kw):
    base = {
        "var_price": Decimal("4330"), "lighter_price": Decimal("4320"),
        "spread_hourly": Decimal("0.0001"), "basis": Decimal("0.002"),
        "var_funding_hourly": Decimal("0.0001"), "lighter_funding_hourly": Decimal("0"),
        "net_funding_hourly_usdt": Decimal("1.2"), "data_errors": {},
        "funding_verified": True, "live_allowed_by_units": True,
        "price_diff_abs": Decimal("10"), "lighter_status": "active",
    }
    base.update(kw)
    return base


class FakeVarGw:
    def __init__(self):
        self.calls = []
        self.pos = D("0")

    def submit_market_order(self, side, qty, *, symbol="XAU", reduce_only=False,
                            max_slippage_pct=D("0.002")):
        self.calls.append((side, reduce_only))
        self.pos += D(qty) if side == "buy" else -D(qty)
        return {"venue": "variational", "symbol": symbol, "side": side,
                "rfq_id": "r1", "status": "accepted", "terminal_ok": False}

    def signed_position(self, symbol):
        return self.pos

    def avg_entry_price(self, symbol):
        return D("4330")


class FakeLitSigner:
    def __init__(self):
        self.pos = D("0")

    def place_market_order(self, symbol, side, qty, ref_price, reduce_only=False, slippage_pct=D("0.002")):
        self.pos += D(qty) if side == "buy" else -D(qty)
        return {"client_order_index": 1, "tx_hash": "0x"}

    def signed_position(self, symbol):
        return self.pos

    def avg_entry_price(self, symbol):
        return D("4320")


def _engine(tmp_path, live=True):
    cfg = dict(CFG_BASE)
    cfg["state_file"] = str(tmp_path / "state.json")
    cfg["log_file"] = str(tmp_path / "log.log")
    cfg["live_trading"] = live
    cfg["fill_confirm_timeout_s"] = 1
    cfg["fill_confirm_poll_s"] = 0
    eng = Engine(cfg)
    # inject fakes + a fixed size_decimals contract, bypass network
    eng._var_gateway = FakeVarGw()
    eng._lighter_signer = FakeLitSigner()
    from rbh_hedge_var.live_executor import LiveExecutor
    eng._live_executor = LiveExecutor(cfg, lighter_signer=eng._lighter_signer, var_gateway=eng._var_gateway)
    eng.lighter.public_contract = lambda sym: {"size_decimals": 4, "market_id": 40, "price_decimals": 2,
                                               "status": "active", "reduce_only": False}
    eng._safe_book = lambda: None
    return eng


def test_entry_stays_shadow_while_guard_armed(tmp_path):
    eng = _engine(tmp_path, live=True)
    # guard armed -> even with live_trading, entry must be shadow (no orders)
    action = eng._do_entry("short_var_long_lighter", "t", _snap())
    assert action.startswith("shadow_open")
    assert eng.sm.state["shadow"] is True


def test_entry_goes_live_when_disarmed_and_gated(tmp_path):
    eng = _engine(tmp_path, live=True)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    action = eng._do_entry("short_var_long_lighter", "t", _snap())
    assert action.startswith("live_open")
    assert eng.sm.state["shadow"] is False
    assert eng.sm.mode == SM.HOLDING
    # variational leg filled first (one non-reduce-only call)
    assert eng._var_gateway.calls[0] == ("sell", False)


def test_live_positions_reconciles_for_live_round(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng.sm.state["shadow"] = False
    eng._var_gateway.pos = D("-2.7")
    eng.lighter.account_snapshot = lambda: {"positions": [{"symbol": "XAU", "qty": D("2.7")}]}
    live = eng._live_positions()
    assert live == {"lighter": D("2.7"), "variational": D("-2.7")}


def test_live_positions_none_for_shadow_round(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng.sm.state["shadow"] = True
    assert eng._live_positions() is None
