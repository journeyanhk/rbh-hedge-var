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


# --- review16 incident fixes: never manage a live round under an armed guard ---

_LEGS = [
    {"venue": "variational", "symbol": "XAU", "side": "sell", "qty": "2.7", "price": "4330"},
    {"venue": "lighter", "symbol": "XAU", "side": "buy", "qty": "2.7", "price": "4320"},
]


def _open_live_round(eng):
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    eng._do_entry("short_var_long_lighter", "t", _snap())
    assert eng.sm.mode == SM.HOLDING and eng.sm.state["shadow"] is False


def test_live_exit_blocked_when_guard_armed_stays_holding(tmp_path):
    # Fix-2: the exact incident — an exit fires on a live round while the guard
    # is armed. Must NOT transition to EXITING then crash; must HALT + stay HOLDING.
    eng = _engine(tmp_path, live=True)
    _open_live_round(eng)
    net_guard.arm()  # restart-while-holding left the guard armed
    action = eng._do_exit("take_profit", _snap())
    assert action.startswith("exit_blocked_guard_armed")
    assert eng.sm.mode == SM.HOLDING, "must not strand in EXITING"
    assert eng.sm.is_halted()
    assert "live_exit_blocked_guard_armed" in (eng.sm.halt_reason() or "")


def test_live_exit_failure_halts_instead_of_crashing(tmp_path):
    # Fix-2 defense-in-depth: a failure DURING the close must fail loud (HALT),
    # not bubble uncaught out of tick() and silently strand EXITING.
    eng = _engine(tmp_path, live=True)
    _open_live_round(eng)

    def boom(*a, **k):
        raise RuntimeError("rfq reject")

    eng._live_executor.close_hedge = boom
    action = eng._do_exit("take_profit", _snap())
    assert action.startswith("exit_error:RuntimeError")
    assert eng.sm.is_halted()
    assert "exit_failed:RuntimeError" in (eng.sm.halt_reason() or "")


def test_startup_halt_when_live_round_but_guard_armed(tmp_path):
    # Fix-1: a persisted LIVE round + armed guard (live=False) = unmanageable.
    eng = _engine(tmp_path, live=True)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(_LEGS)
    eng.sm.state["shadow"] = False
    eng.sm.save()
    reason = eng.halt_if_unmanageable_live_round(live=False)
    assert reason and reason.startswith("live_round_guard_armed:HOLDING")
    assert eng.sm.is_halted()


def test_startup_no_halt_when_live_armed(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(_LEGS)
    eng.sm.state["shadow"] = False
    eng.sm.save()
    assert eng.halt_if_unmanageable_live_round(live=True) is None
    assert not eng.sm.is_halted()


def test_startup_no_halt_for_shadow_round(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(_LEGS)  # shadow defaults True
    assert eng.halt_if_unmanageable_live_round(live=False) is None
    assert not eng.sm.is_halted()


# --- var-fq1 Fix-1: preflight recognises its own persisted live position -----

def _match_positions(eng):
    # on-venue book == _LEGS (var sell 2.7 -> -2.7, lit buy 2.7 -> +2.7)
    eng._var_gateway.pos = D("-2.7")
    eng.lighter.account_snapshot = lambda: {"positions": [{"symbol": "XAU", "qty": D("2.7")}]}


def test_preflight_resumes_persisted_live_round(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng.fetch_snapshot = lambda: _snap()
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(_LEGS)
    eng.sm.state["shadow"] = False
    eng.sm.save()
    _match_positions(eng)
    checks = {c["check"]: c for c in eng.preflight()}
    assert checks["book_flat"]["ok"] is True
    assert "resuming persisted live round" in checks["book_flat"]["detail"]


def test_preflight_fails_on_orphan_residual(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng.fetch_snapshot = lambda: _snap()
    # residual present but NO persisted live round (fresh IDLE state)
    _match_positions(eng)
    checks = {c["check"]: c for c in eng.preflight()}
    assert checks["book_flat"]["ok"] is False
    assert "unexpected residual" in checks["book_flat"]["detail"]


# --- var-fq1 Fix-2: EXITING recovery resumes the close on a matching residual -

def _exiting_live_round(eng):
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    eng.fetch_snapshot = lambda: _snap()
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(_LEGS)
    eng.sm.state["shadow"] = False
    eng.sm.begin_exit("mid")
    assert eng.sm.mode == SM.EXITING


def test_recover_exiting_resumes_close_on_match(tmp_path):
    eng = _engine(tmp_path, live=True)
    _exiting_live_round(eng)
    _match_positions(eng)
    eng._recover(SM.EXITING)
    assert eng.sm.mode == SM.COOLDOWN, "resume-close should finish the round"
    assert not eng.sm.is_halted()


def test_recover_exiting_halts_on_position_mismatch(tmp_path):
    eng = _engine(tmp_path, live=True)
    _exiting_live_round(eng)
    # variational leg present but Lighter leg missing -> mismatch (possible orphan)
    eng._var_gateway.pos = D("-2.7")
    eng.lighter.account_snapshot = lambda: {"positions": []}
    eng._recover(SM.EXITING)
    assert eng.sm.is_halted()
    assert "recovery_residual_position" in (eng.sm.halt_reason() or "")


def test_recover_exiting_halts_when_guard_armed_despite_match(tmp_path):
    # even a matching residual must NOT be traded while the guard is armed
    eng = _engine(tmp_path, live=True)
    _exiting_live_round(eng)
    net_guard.arm()  # re-lock: cannot trade
    _match_positions(eng)
    eng._recover(SM.EXITING)
    assert eng.sm.is_halted()
    assert eng.sm.mode == SM.EXITING


# --- var-desgin6: trading-hours session gate (force-exit before market close) ---

def test_hold_tick_force_exits_before_market_close(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng._do_entry("short_var_long_lighter", "t", _snap())   # shadow (guard armed)
    assert eng.sm.mode == SM.HOLDING
    snap = _snap(var_session_enabled=True, var_market_open=True,
                 var_seconds_to_close=600)   # < close_buffer_seconds (1800)
    action = eng._hold_tick(snap)
    assert "market_closing" in action
    assert eng.sm.mode == SM.COOLDOWN


def test_hold_tick_no_force_exit_with_ample_time(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng._do_entry("short_var_long_lighter", "t", _snap())
    snap = _snap(var_session_enabled=True, var_market_open=True,
                 var_seconds_to_close=6 * 3600)
    action = eng._hold_tick(snap)
    assert action == "holding"
    assert eng.sm.mode == SM.HOLDING


# --- review17: frozen-market alert latch (P1-1) + metadata overlay (P1-2) ---

def _capture_alerts(eng):
    sent = []
    eng._alert = lambda text: sent.append(text)
    return sent


def test_frozen_market_alerts_once_not_every_tick(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng._do_entry("short_var_long_lighter", "t", _snap())
    sent = _capture_alerts(eng)
    closed = _snap(var_session_enabled=True, var_market_open=False, var_seconds_to_close=0)
    # three consecutive frozen ticks -> exactly one alert (heartbeat not yet due)
    for _ in range(3):
        assert eng._hold_tick(closed) == "session_frozen"
    assert len(sent) == 1
    assert "FROZEN" in sent[0]
    assert eng.sm.mode == SM.HOLDING  # cannot exit a frozen leg


def test_frozen_market_heartbeat_after_interval(tmp_path):
    import time
    eng = _engine(tmp_path, live=True)
    eng.cfg["variational"]["trading_hours"]["frozen_alert_interval_s"] = 3600
    eng._do_entry("short_var_long_lighter", "t", _snap())
    sent = _capture_alerts(eng)
    closed = _snap(var_session_enabled=True, var_market_open=False, var_seconds_to_close=0)
    eng._hold_tick(closed)              # first alert, latch = now
    assert len(sent) == 1
    # push the latch beyond the heartbeat interval -> next frozen tick heartbeats
    eng.sm.state["session_frozen_alerted_at"] = int(time.time()) - 4000
    eng._hold_tick(closed)
    assert len(sent) == 2  # first + heartbeat


def test_frozen_latch_cleared_on_reopen(tmp_path):
    eng = _engine(tmp_path, live=True)
    eng._do_entry("short_var_long_lighter", "t", _snap())
    sent = _capture_alerts(eng)
    eng._hold_tick(_snap(var_session_enabled=True, var_market_open=False, var_seconds_to_close=0))
    assert eng.sm.state.get("session_frozen_alerted_at") is not None
    # reopen with ample time -> latch cleared + one reopen note, round continues
    action = eng._hold_tick(_snap(var_session_enabled=True, var_market_open=True,
                                  var_seconds_to_close=6 * 3600))
    assert action == "holding"
    assert eng.sm.state.get("session_frozen_alerted_at") is None
    assert any("REOPENED" in m for m in sent)


def test_metadata_overlay_forces_earlier_close(tmp_path):
    # calendar says open with plenty of time, but venue metadata says close in
    # 10min (holiday early-close) -> overlay must flatten now.
    import datetime
    eng = _engine(tmp_path, live=True)
    # Two windows covering the entire week so the CALENDAR is always open,
    # isolating the metadata overlay from wall-clock flakiness.
    eng._var_hours = {"enabled": True, "open_windows": [
        {"open": "Mon 00:00", "close": "Thu 00:00"},
        {"open": "Thu 00:00", "close": "Mon 00:00"},
    ]}
    now = datetime.datetime.now(datetime.timezone.utc)
    var_asset = {"raw": {"next_close_at": int(now.timestamp()) + 600}}  # 10min < 1800 buffer
    sess = eng._var_session(var_asset)
    assert sess["open"] is True
    assert sess["seconds_to_close"] <= 600
    assert sess.get("seconds_to_close_source") == "metadata"


# --- review19: HALT must flatten first, and must not blind an open round ------

def test_drawdown_halt_flattens_before_latching(tmp_path):
    # review19: a drawdown HALT that trips while HOLDING must CLOSE the round
    # first (so the leg isn't left naked vs the 24/7 Lighter side), THEN latch.
    eng = _engine(tmp_path, live=True)
    _open_live_round(eng)
    _match_positions(eng)
    eng.fetch_snapshot = lambda: _snap()
    # push today's PnL below the daily-loss floor
    eng.sm.state["daily_pnl"] = {}
    from rbh_hedge_var.state_machine import _utc_day
    eng.sm.state["daily_pnl"][_utc_day()] = -999.0
    sent = _capture_alerts(eng)
    out = eng.tick()
    assert eng.sm.is_halted()
    # round was flattened (not stranded in HOLDING/ENTERING)
    assert eng.sm.mode in (SM.COOLDOWN, SM.IDLE), f"expected flattened, got {eng.sm.mode}"
    assert any("flatten" in m.lower() for m in sent)
    assert out["halt"]


def test_halted_holding_round_still_marks_to_market(tmp_path):
    # review19: HALT stops OPENING new rounds; it must NOT blind the risk view of
    # a round we are still carrying. A halted+HOLDING tick populates live MTM.
    eng = _engine(tmp_path, live=True)
    _open_live_round(eng)
    eng.fetch_snapshot = lambda: _snap()
    eng.sm.set_halt("manual_test")
    assert eng.sm.mode == SM.HOLDING and eng.sm.is_halted()
    out = eng.tick()
    assert out["action"] == "halted"
    snap = out["snapshot"]
    # read-only MTM populated the unrealized fields for the dashboard
    assert "unrealized_total_pnl_usdt" in snap or "round_pnl_vs_entry_usdt" in snap
