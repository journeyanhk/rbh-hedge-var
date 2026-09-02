import time

from rbh_hedge_var.state_machine import (
    StateMachine, IDLE, ENTERING, HOLDING, EXITING, COOLDOWN,
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

    sm.finish_exit(1.25, "reversal", cooldown_s=1)
    assert sm.mode == COOLDOWN
    assert sm.state["realized_pnl"] == 1.25
    assert len(sm.state["round_history"]) == 1

    # cooldown not elapsed yet
    assert sm.maybe_leave_cooldown() is False
    time.sleep(1.1)
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
