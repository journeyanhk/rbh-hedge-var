import time

from rbh_hedge_var.state_machine import (
    COOLDOWN,
    ENTERING,
    EXITING,
    HOLDING,
    IDLE,
    StateMachine,
)


def _sm(tmp_path):
    return StateMachine(str(tmp_path / "state.json"))


def test_full_lifecycle(tmp_path):
    sm = _sm(tmp_path)
    assert sm.mode == IDLE

    sm.begin_entry("short_var_long_lighter", "signal")
    assert sm.mode == ENTERING

    legs = [{"venue": "variational", "side": "sell", "qty": "2", "price": "4327"},
            {"venue": "lighter", "side": "buy", "qty": "2", "price": "4323"}]
    sm.confirm_hold(legs)
    assert sm.mode == HOLDING
    assert sm.state["round_id"] == 1

    sm.begin_exit("reversal")
    assert sm.mode == EXITING

    sm.finish_exit(1.25, 0.75, "reversal", cooldown_s=1)
    assert sm.mode == COOLDOWN
    assert sm.state["realized_pnl"] == 2.0
    assert len(sm.state["round_history"]) == 1
    assert sm.state["round_history"][0]["price_pnl"] == 1.25
    assert sm.state["round_history"][0]["funding_pnl"] == 0.75
    assert sm.state["round_history"][0]["pnl"] == 2.0

    # cooldown not elapsed yet
    assert sm.maybe_leave_cooldown() is False
    time.sleep(1.1)
    assert sm.maybe_leave_cooldown() is True
    assert sm.mode == IDLE


def test_force_leave_cooldown(tmp_path):
    sm = _sm(tmp_path)
    sm.begin_entry("var_short_lit_long", "signal")
    sm.confirm_hold([{"venue": "variational", "side": "sell", "qty": "2", "price": "4321"}])
    sm.begin_exit("reversal")
    sm.finish_exit(1.0, 0.0, "reversal", cooldown_s=100000)
    assert sm.mode == COOLDOWN
    # config change (future cooldowns) does not shorten this in-progress one:
    assert sm.maybe_leave_cooldown() is False
    # operator override ends it immediately.
    assert sm.force_leave_cooldown() is True
    assert sm.mode == IDLE
    assert sm.state["cooldown_until"] is None
    # idempotent: nothing to leave when already IDLE.
    assert sm.force_leave_cooldown() is False


def test_clamp_cooldown_shortens_in_progress(tmp_path):
    sm = _sm(tmp_path)
    sm.begin_entry("var_short_lit_long", "signal")
    sm.confirm_hold([{"venue": "variational", "side": "sell", "qty": "2", "price": "4321"}])
    sm.begin_exit("reversal")
    sm.finish_exit(1.0, 0.0, "reversal", cooldown_s=100000)   # 27h stale cooldown
    assert sm.mode == COOLDOWN
    # config lowered to 5s: clamp brings the absolute deadline down.
    assert sm.clamp_cooldown(5) is True
    assert int(sm.state["cooldown_until"]) <= int(time.time()) + 5
    # clamp never EXTENDS.
    assert sm.clamp_cooldown(100000) is False
    # after the (now short) window elapses it leaves cooldown.
    time.sleep(5.1)
    assert sm.maybe_leave_cooldown() is True
    assert sm.mode == IDLE


def test_illegal_transition_raises(tmp_path):
    sm = _sm(tmp_path)
    try:
        sm.transition(HOLDING, "skip entering")
        assert False, "expected illegal transition"
    except ValueError:
        pass


def test_entry_abort_returns_to_idle(tmp_path):
    sm = _sm(tmp_path)
    sm.begin_entry("short_var_long_lighter", "signal")
    sm.abort_entry("leg_not_filled")
    assert sm.mode == IDLE
    assert sm.state["legs"] == []


def test_persistence_survives_reload(tmp_path):
    path = tmp_path / "state.json"
    sm = StateMachine(str(path))
    sm.begin_entry("short_lighter_long_var", "x")
    sm2 = StateMachine(str(path))
    assert sm2.mode == ENTERING
    assert sm2.state["direction"] == "short_lighter_long_var"


def test_reversal_streak(tmp_path):
    sm = _sm(tmp_path)
    assert sm.bump_reversal(True) == 1
    assert sm.bump_reversal(True) == 2
    assert sm.bump_reversal(False) == 0


def test_funding_accrues_and_books_at_exit(tmp_path):
    sm = _sm(tmp_path)
    legs = [{"venue": "variational", "side": "sell", "qty": "2", "price": "4327"},
            {"venue": "lighter", "side": "buy", "qty": "2", "price": "4323"}]
    sm.begin_entry("short_var_long_lighter", "signal")
    sm.confirm_hold(legs)
    assert sm.funding_accrued() == 0.0
    sm.accrue_funding(0.5)
    sm.accrue_funding(0.25)
    assert sm.funding_accrued() == 0.75
    sm.begin_exit("reversal")
    sm.finish_exit(-1.0, sm.funding_accrued(), "reversal", cooldown_s=1)
    # price leg lost 1.0 but funding earned 0.75 -> net -0.25
    assert sm.state["realized_pnl"] == -0.25
    # funding reset for next round
    assert sm.funding_accrued() == 0.0


def test_shadow_rounds_jsonl_is_append_only(tmp_path):
    import json
    sm = _sm(tmp_path)
    legs = [{"venue": "variational", "side": "sell", "qty": "1", "price": "4327"}]
    for _ in range(3):
        sm.begin_entry("short_var_long_lighter", "s")
        sm.confirm_hold(legs)
        sm.begin_exit("r")
        sm.finish_exit(0.1, 0.2, "r", cooldown_s=0)
        sm.maybe_leave_cooldown()
    ledger = tmp_path / "shadow_rounds.jsonl"
    lines = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert len(lines) == 3
    assert all(r["funding_pnl"] == 0.2 for r in lines)


def test_halt_latches_and_clears(tmp_path):
    sm = _sm(tmp_path)
    assert sm.is_halted() is False
    assert sm.set_halt("drawdown") is True     # new halt
    assert sm.set_halt("drawdown") is False    # already halted -> not new
    assert sm.is_halted() is True
    assert sm.halt_reason() == "drawdown"
    # survives reload
    sm2 = StateMachine(str(tmp_path / "state.json"))
    assert sm2.is_halted() is True
    sm2.clear_halt()
    assert sm2.is_halted() is False
