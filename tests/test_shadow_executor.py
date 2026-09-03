"""ShadowExecutor — pure-pricing simulator, guard-independent (review13).

The three methods used to assert ``net_guard.is_armed()``. That crashed every
tick when a LIVE deploy (guard disarmed) carried a leftover shadow HOLDING round
persisted in state.json. The simulator sends no orders, so it must run
regardless of guard state — safety lives in net_guard's transport layer and the
real gateways, not in this pricing-only class.
"""
import json
from pathlib import Path

from rbh_hedge_var import net_guard
from rbh_hedge_var.numeric import D
from rbh_hedge_var.shadow_executor import ShadowExecutor

CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
BOOK = {"bids": [(D("4325"), D("5"))], "asks": [(D("4326"), D("5"))]}


def teardown_function():
    net_guard.arm()   # restore the default safe state for other tests


def test_shadow_executor_runs_after_disarm():
    # Reproduces the review13 crash: guard DOWN (live deploy) + shadow round.
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    assert not net_guard.is_armed()
    ex = ShadowExecutor(CFG)

    opened = ex.open_hedge("short_var_long_lighter", D("300"),
                           D("4428"), D("4425"), D("0.0001"), BOOK)
    assert opened["both_filled"] is True
    legs = opened["legs"]

    # mark_to_market was the exact method the log showed crashing every 60s.
    mtm = ex.mark_to_market(legs, D("4429"), D("4426"), BOOK)
    assert mtm is not None

    closed = ex.close_hedge(legs, D("4429"), D("4426"), BOOK)
    assert closed["shadow"] is True
    assert "price_pnl" in closed


def test_shadow_executor_also_runs_while_armed():
    net_guard.arm()
    ex = ShadowExecutor(CFG)
    opened = ex.open_hedge("short_lighter_long_var", D("300"),
                           D("4425"), D("4428"), D("0.0001"), BOOK)
    assert opened["both_filled"] is True
