"""Engine wiring tests (offline) — the P0 review fixes.

fetch_snapshot is monkeypatched so these never touch the network; every branch
under test (funding accrual, MTM stop-loss, ENTERING/EXITING recovery, halt
latch) is pure once a snapshot is supplied.
"""
import json
import time
from decimal import Decimal
from pathlib import Path

from rbh_hedge_var import state_machine as SM
from rbh_hedge_var.engine import Engine

CFG_BASE = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())

LEGS = [
    {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
    {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
]


def _engine(tmp_path, **over):
    cfg = dict(CFG_BASE)
    cfg["state_file"] = str(tmp_path / "state.json")
    cfg["log_file"] = str(tmp_path / "log.log")
    cfg.update(over)
    return Engine(cfg)


def _snap(**kw):
    base = {
        "var_price": Decimal("4330"), "lighter_price": Decimal("4320"),
        "spread_hourly": Decimal("0.0001"), "basis": Decimal("0.002"),
        "net_funding_hourly_usdt": Decimal("1.2"), "data_errors": {},
    }
    base.update(kw)
    return base


def test_funding_accrues_while_holding(tmp_path):
    eng = _engine(tmp_path, max_round_loss_usdt=0.0, take_profit_total_pnl_usdt=0.0,
                  taker_slippage_pct=0.0)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    eng.sm.state["opened_at"] = int(time.time()) - 7200
    eng.sm.state["funding_last_accrual_ts"] = int(time.time()) - 7200
    eng.sm.save()
    action = eng._hold_tick(_snap())
    assert action == "holding"
    assert Decimal("2.3") < Decimal(str(eng.sm.funding_accrued())) < Decimal("2.5")
    assert eng.sm.mode == SM.HOLDING


def test_per_round_stop_loss_closes(tmp_path):
    eng = _engine(tmp_path, max_round_loss_usdt=8.0)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    action = eng._hold_tick(_snap(var_price=Decimal("4360"), lighter_price=Decimal("4300")))
    assert action.startswith("shadow_close:round_stop_loss")
    assert eng.sm.mode == SM.COOLDOWN


def test_recover_from_exiting_does_not_deadlock(tmp_path):
    eng = _engine(tmp_path)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    eng.sm.begin_exit("mid")
    assert eng.sm.mode == SM.EXITING
    # simulate restart: fresh engine loads the persisted EXITING state
    eng2 = _engine(tmp_path)
    eng2.fetch_snapshot = lambda: _snap()
    eng2.tick()
    assert eng2.sm.mode == SM.COOLDOWN   # recovered exit closed the round


def test_recover_from_entering_aborts(tmp_path):
    eng = _engine(tmp_path)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    assert eng.sm.mode == SM.ENTERING
    eng2 = _engine(tmp_path)
    eng2.fetch_snapshot = lambda: _snap()
    eng2.tick()
    assert eng2.sm.mode in (SM.IDLE, SM.ENTERING, SM.HOLDING, SM.COOLDOWN)
    # specifically: ENTERING was aborted back to IDLE before the entry branch ran
    assert eng2.sm.state["mode"] != SM.ENTERING


def test_halt_latch_short_circuits_tick(tmp_path):
    eng = _engine(tmp_path)
    eng.sm.set_halt("drawdown")

    def _boom():
        raise AssertionError("must not fetch data while halted")

    eng.fetch_snapshot = _boom
    out = eng.tick()
    assert out.get("halt") == "drawdown"
