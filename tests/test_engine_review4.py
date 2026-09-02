"""review4 P0-C/P0-D engine wiring: live recovery HALT, IDLE flat-check, attest."""
import json
from pathlib import Path

from rbh_hedge_var import funding_attest, net_guard
from rbh_hedge_var import state_machine as SM
from rbh_hedge_var.engine import Engine
from rbh_hedge_var.numeric import D

CFG_BASE = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


class FakeVarGw:
    def __init__(self, pos=D("0")):
        self.pos = D(pos)

    def signed_position(self, symbol):
        return self.pos


def _engine(tmp_path, live=True):
    cfg = dict(CFG_BASE)
    cfg["state_file"] = str(tmp_path / "state.json")
    cfg["log_file"] = str(tmp_path / "log.log")
    cfg["live_trading"] = live
    eng = Engine(cfg)
    eng.lighter.public_contract = lambda sym: {"size_decimals": 4, "market_id": 40}
    return eng


def test_live_recover_entering_flat_aborts_to_idle(tmp_path):
    eng = _engine(tmp_path)
    eng._var_gateway = FakeVarGw(D("0"))
    eng.lighter.account_snapshot = lambda: {"positions": []}
    eng.sm.state["mode"] = SM.ENTERING
    eng.sm.state["shadow"] = False
    eng._recover(SM.ENTERING)
    assert eng.sm.mode == SM.IDLE
    assert not eng.sm.is_halted()


def test_live_recover_residual_position_halts(tmp_path):
    eng = _engine(tmp_path)
    eng._var_gateway = FakeVarGw(D("-2.7"))   # naked variational leg
    eng.lighter.account_snapshot = lambda: {"positions": []}
    eng.sm.state["mode"] = SM.ENTERING
    eng.sm.state["shadow"] = False
    eng._recover(SM.ENTERING)
    assert eng.sm.is_halted()
    assert "residual" in (eng.sm.halt_reason() or "")


def test_live_recover_reconcile_failure_halts(tmp_path):
    eng = _engine(tmp_path)

    class DeadGw:
        def signed_position(self, symbol):
            raise RuntimeError("venue down")

    eng._var_gateway = DeadGw()
    eng.lighter.account_snapshot = lambda: {"positions": []}
    eng.sm.state["mode"] = SM.EXITING
    eng.sm.state["shadow"] = False
    eng._recover(SM.EXITING)
    assert eng.sm.is_halted()


def test_shadow_recover_entering_aborts_without_reconcile(tmp_path):
    eng = _engine(tmp_path)
    eng._var_gateway = None
    eng.sm.state["mode"] = SM.ENTERING
    eng.sm.state["shadow"] = True
    eng._recover(SM.ENTERING)
    assert eng.sm.mode == SM.IDLE
    assert not eng.sm.is_halted()


def test_idle_flat_check_halts_on_residual(tmp_path):
    eng = _engine(tmp_path)
    eng.cfg["idle_reconcile_every_ticks"] = 1
    eng._var_gateway = FakeVarGw(D("2.7"))
    eng.lighter.account_snapshot = lambda: {"positions": []}
    action = eng._idle_flat_check()
    assert action == "idle_flat_check_halt"
    assert eng.sm.is_halted()


def test_idle_flat_check_noop_when_flat(tmp_path):
    eng = _engine(tmp_path)
    eng.cfg["idle_reconcile_every_ticks"] = 1
    eng._var_gateway = FakeVarGw(D("0"))
    eng.lighter.account_snapshot = lambda: {"positions": []}
    assert eng._idle_flat_check() is None
    assert not eng.sm.is_halted()


def test_idle_flat_check_noop_in_shadow(tmp_path):
    eng = _engine(tmp_path, live=False)
    assert eng._idle_flat_check() is None


def test_attested_lighter_interval_reads_state(tmp_path):
    eng = _engine(tmp_path)
    att = funding_attest.build_attestation("lighter", 3600, samples=6, detail="ok")
    eng.sm.set_funding_attestation(att)
    assert eng._attested_lighter_interval() == 3600
    # clearing removes it
    eng.sm.set_funding_attestation(None)
    assert eng._attested_lighter_interval() is None


def test_verify_funding_writes_attestation(tmp_path):
    eng = _engine(tmp_path)
    rows = [{"timestamp": 1_700_000_000 + i * 3600, "amount": "0.5"} for i in range(6)]
    eng.lighter.funding_history = lambda symbol, limit=200, auth_token=None: rows
    eng.lighter.funding_rate = lambda symbol: {"rate": D("0.0001")}
    out = eng.verify_funding()
    assert out["ok"] is True
    assert eng.sm.funding_attestation()["interval_s"] == 3600


def test_verify_funding_rejects_bad_cadence(tmp_path):
    eng = _engine(tmp_path)
    rows = [{"timestamp": 1_700_000_000 + i * 14400, "amount": "0.5"} for i in range(6)]
    eng.lighter.funding_history = lambda symbol, limit=200, auth_token=None: rows
    eng.lighter.funding_rate = lambda symbol: {"rate": D("0.0001")}
    out = eng.verify_funding()
    assert out["ok"] is False
    assert eng.sm.funding_attestation() is None


def test_live_positions_streak_holds_then_forces_exit(tmp_path):
    # P1-1: a single transient reconcile failure keeps holding (returns None);
    # only after `reconcile_fail_streak` consecutive failures does it escalate
    # to a sentinel imbalance that forces the watchdog to exit.
    eng = _engine(tmp_path)
    eng.cfg["reconcile_fail_streak"] = 3
    eng.sm.state["shadow"] = False

    class DeadGw:
        def signed_position(self, symbol):
            raise RuntimeError("venue down")

    eng._var_gateway = DeadGw()
    eng.lighter.account_snapshot = lambda: {"positions": []}
    assert eng._live_positions() is None   # 1
    assert eng._live_positions() is None   # 2
    sentinel = eng._live_positions()       # 3 -> escalate
    assert sentinel == {"variational": D("0"), "lighter": D("0")}


def test_do_entry_naked_leg_halts(tmp_path):
    eng = _engine(tmp_path)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")

    class NakedExec:
        def open_hedge(self, *a, **k):
            from rbh_hedge_var.live_executor import NakedLegError
            raise NakedLegError("Lighter hedge unconfirmed; flattened Variational leg")

    eng._live_executor = NakedExec()
    eng._var_gateway = FakeVarGw(D("0"))
    snap = {"var_price": D("4330"), "lighter_price": D("4320"),
            "live_allowed_by_units": True, "funding_verified": True}
    action = eng._do_entry("short_var_long_lighter", "t", snap)
    assert action.startswith("entry_naked_leg")
    assert eng.sm.is_halted()
    assert "naked_leg" in (eng.sm.halt_reason() or "")
