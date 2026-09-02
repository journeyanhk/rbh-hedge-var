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
    eng = Engine(cfg)
    # Hermetic: these tests assert on fixed leg prices, so the MTM close must use
    # the reference price, NOT the LIVE Lighter order book. Without this stub
    # `_hold_tick`/`_close_and_finish` fetch the real book over the network and a
    # market move (live XAU != the hard-coded leg price) flips the sign of the
    # entry baseline and makes the suite flaky.
    eng._safe_book = lambda: None
    # Hermetic: the lighter contract (size step, decimals) is a live REST call.
    # Unit tests assert on fixed legs, so pin a deterministic offline contract.
    eng.lighter.public_contract = lambda symbol: {
        "venue": "lighter", "symbol": str(symbol).upper(), "market_id": 0,
        "mark_price": Decimal("4320"), "index_price": Decimal("4320"),
        "last_price": Decimal("4320"), "size_decimals": 4, "price_decimals": 2,
        "min_base_amount": Decimal("0"), "min_quote_amount": Decimal("0"),
        "maker_fee": Decimal("0"), "taker_fee": Decimal("0"),
        "multiplier": Decimal("1"), "status": "active", "reduce_only": False,
    }
    return eng


def _snap(**kw):
    base = {
        "var_price": Decimal("4330"), "lighter_price": Decimal("4320"),
        "spread_hourly": Decimal("0.0001"), "basis": Decimal("0.002"),
        # per-leg hourly funding (var - lit = 0.0001 -> 1.2/h edge @ 12k)
        "var_funding_hourly": Decimal("0.0001"), "lighter_funding_hourly": Decimal("0"),
        "net_funding_hourly_usdt": Decimal("1.2"), "data_errors": {},
    }
    base.update(kw)
    return base


def test_funding_accrues_while_holding(tmp_path):
    eng = _engine(tmp_path, max_round_loss_usdt=0.0, take_profit_total_pnl_usdt=0.0,
                  taker_slippage_pct=0.0, notional_per_leg_usdt=12000.0)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    eng.sm.state["opened_at"] = int(time.time()) - 7200
    eng.sm.state["funding_last_accrual_ts"] = int(time.time()) - 7200
    eng.sm.save()
    action = eng._hold_tick(_snap())
    assert action == "holding"
    assert Decimal("2.3") < Decimal(str(eng.sm.funding_accrued())) < Decimal("2.5")
    assert eng.sm.mode == SM.HOLDING


def test_funding_accrues_negative_after_reversal(tmp_path):
    # P0-5: still holding short_var_long_lighter but the spread has flipped
    # (lighter now richer). The held position is PAYING funding, so the accrual
    # increment must be negative, not the unsigned entry edge.
    eng = _engine(tmp_path, max_round_loss_usdt=0.0, take_profit_total_pnl_usdt=0.0,
                  taker_slippage_pct=0.0, force_exit_basis_pct=0.9,
                  exit_on_spread_reversal=False, notional_per_leg_usdt=12000.0)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    eng.sm.state["opened_at"] = int(time.time()) - 3600
    eng.sm.state["funding_last_accrual_ts"] = int(time.time()) - 3600
    eng.sm.save()
    # var 0.00001, lit 0.00009 -> held = (0.00001-0.00009)*12000 = -0.96/h
    snap = _snap(spread_hourly=Decimal("-0.00008"),
                 var_funding_hourly=Decimal("0.00001"),
                 lighter_funding_hourly=Decimal("0.00009"))
    eng._hold_tick(snap)
    assert eng.sm.funding_accrued() < 0, "reversal must accrue negative funding"
    assert Decimal("-1.0") < Decimal(str(eng.sm.funding_accrued())) < Decimal("-0.9")


def test_recover_exit_deferred_on_zero_price(tmp_path):
    # P1-9: a crash-restart with an empty recovery snapshot must NOT settle at
    # price 0 (which would book a huge fake loss). Stay EXITING and retry.
    eng = _engine(tmp_path)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    eng.sm.begin_exit("mid")
    action = eng._close_and_finish("recovered_exit", {})  # no prices
    assert action == "exit_deferred:recovered_exit"
    assert eng.sm.mode == SM.EXITING
    assert eng.sm.today_pnl() == 0.0  # nothing booked, no fake loss
    # next tick with real prices completes the exit
    action2 = eng._close_and_finish("recovered_exit",
                                    {"var_price": Decimal("4330"), "lighter_price": Decimal("4320")})
    assert action2.startswith("shadow_close")
    assert eng.sm.mode == SM.COOLDOWN


def test_no_stop_loss_on_fresh_open(tmp_path):
    # review3 P0: a freshly opened round carries ~2x taker slippage as a sunk
    # cost baseline; the per-round stop must NOT fire on tick #1 with no market move.
    eng = _engine(tmp_path, max_round_loss_usdt=8.0)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    # entry prices == leg fill prices, no adverse move
    action = eng._hold_tick(_snap(var_price=Decimal("4330"), lighter_price=Decimal("4320")))
    assert action == "holding", f"should hold, got {action}"
    assert eng.sm.mode == SM.HOLDING
    # baseline captured the sunk cost (negative), adverse ~ 0
    assert eng.sm.state["entry_mtm_usdt"] < 0


def test_per_round_stop_loss_closes_on_adverse_move(tmp_path):
    eng = _engine(tmp_path, max_round_loss_usdt=8.0)
    eng.sm.begin_entry("short_var_long_lighter", "t")
    eng.sm.confirm_hold(LEGS)
    # tick 1 at entry prices establishes the baseline, holds
    assert eng._hold_tick(_snap(var_price=Decimal("4330"), lighter_price=Decimal("4320"))) == "holding"
    # tick 2: big adverse move (short var up, long lighter down) -> deterioration
    # relative to baseline exceeds $8 -> stop-loss
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


def test_halt_keeps_snapshot_live_but_skips_trading(tmp_path):
    # review3: under HALT the dashboard must stay live (snapshot fetched) while
    # all trading is skipped. The old behavior froze the snapshot to a row of "-".
    eng = _engine(tmp_path)
    eng.sm.set_halt("drawdown")
    calls = {"n": 0}

    def _snap_counting():
        calls["n"] += 1
        return _snap()

    eng.fetch_snapshot = _snap_counting
    out = eng.tick()
    assert calls["n"] == 1, "snapshot must be fetched even while halted"
    assert out.get("halt") == "drawdown"
    assert out.get("action") == "halted"
    assert out.get("snapshot"), "snapshot must be returned for the dashboard"


def test_clear_halt_resets_ledger_and_resumes(tmp_path):
    eng = _engine(tmp_path)
    eng.sm.state["daily_pnl"] = {"2026-09-02": -28.5}
    eng.sm.state["realized_pnl"] = -28.5
    eng.sm.set_halt("daily loss -28.5 breached limit -50")
    prior = eng.sm.clear_halt_and_ledger()
    assert prior["halt"]["reason"].startswith("daily loss")
    assert eng.sm.is_halted() is False
    assert eng.sm.state["realized_pnl"] == 0.0
    assert eng.sm.state["daily_pnl"] == {}
    # a subsequent tick no longer short-circuits on halt (spread None blocks entry
    # so the tick stays offline/deterministic)
    eng.fetch_snapshot = lambda: _snap(spread_hourly=None)
    out = eng.tick()
    assert out.get("halt") in (None, "")
    assert str(out.get("action", "")).startswith("no_entry")
