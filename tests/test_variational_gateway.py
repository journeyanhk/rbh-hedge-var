"""VariationalOrderGateway — guard, creds, RFQ fill normalization, positions."""
import pytest

from rbh_hedge_var import net_guard
from rbh_hedge_var.net_guard import WriteBlockedError
from rbh_hedge_var.numeric import D
from rbh_hedge_var.variational_gateway import VariationalGatewayError, VariationalOrderGateway


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


def _gw(monkeypatch, creds=True):
    gw = VariationalOrderGateway(symbol="XAU", env_file=".env")
    gw._api_key = "k" if creds else ""
    gw._api_secret = "s" if creds else ""
    return gw


def test_place_blocked_while_armed(monkeypatch):
    gw = _gw(monkeypatch)
    with pytest.raises(WriteBlockedError):
        gw.place_taker_order("sell", D("2.7"))


def test_missing_creds_fail_closed(monkeypatch):
    gw = _gw(monkeypatch, creds=False)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(VariationalGatewayError):
        gw.place_taker_order("sell", D("2.7"))


def test_place_taker_order_happy_path(monkeypatch):
    gw = _gw(monkeypatch)
    posts = []

    def fake_post(path, body):
        posts.append((path, body))
        if "quote" in path:
            return {"quote_id": "q1", "price": "4330.5"}
        return {"status": "filled", "filled_quantity": "2.7", "fill_price": "4330.4", "order_id": "o1"}

    monkeypatch.setattr(gw, "_post", fake_post)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = gw.place_taker_order("sell", D("2.7"), symbol="XAU")
    assert out["filled_qty"] == D("2.7")
    assert out["filled_price"] == D("4330.4")
    assert out["order_id"] == "o1"
    assert len(posts) == 2  # request_quote then accept_quote


def test_place_taker_order_unfilled_raises(monkeypatch):
    gw = _gw(monkeypatch)

    def fake_post(path, body):
        if "quote" in path:
            return {"quote_id": "q1", "price": "4330"}
        return {"status": "rejected", "filled_quantity": "0"}

    monkeypatch.setattr(gw, "_post", fake_post)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(VariationalGatewayError):
        gw.place_taker_order("sell", D("2.7"))


def test_signed_position_short_is_negative(monkeypatch):
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: {
        "positions": [{"asset": "XAU", "side": "short", "net_quantity": "2.7"}]})
    assert gw.signed_position("XAU") == D("-2.7")
