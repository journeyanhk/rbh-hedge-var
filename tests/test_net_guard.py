import pytest

from rbh_hedge_var import net_guard
from rbh_hedge_var.net_guard import WriteBlockedError


def setup_function():
    net_guard.arm()


def test_get_allowed_when_armed():
    net_guard.check("GET", "https://api.rh.lighter.xyz/api/v1/orderBookDetails")  # no raise


def test_post_blocked_when_armed():
    with pytest.raises(WriteBlockedError):
        net_guard.check("POST", "https://api.rh.lighter.xyz/api/v1/sendTx")


def test_disarm_requires_confirmation():
    with pytest.raises(WriteBlockedError):
        net_guard.disarm("please")
    assert net_guard.is_armed()


def test_disarm_then_post_allowed_then_rearm():
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    net_guard.check("POST", "https://example/x")  # no raise
    net_guard.arm()
    with pytest.raises(WriteBlockedError):
        net_guard.check("POST", "https://example/x")
