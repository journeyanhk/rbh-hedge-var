"""VariationalOrderGateway — guard, creds, order acceptance, positions.

review4 P0-A/P0-B: an order response is an ACCEPTANCE, never a proven fill, so
the gateway returns {rfq_id, status, ref_price, terminal_ok} and leaves fill
truth to position reconciliation. Token vr-token Cookie transport is the default scheme.
"""
import pytest

from rbh_hedge_var import net_guard
from rbh_hedge_var.net_guard import WriteBlockedError
from rbh_hedge_var.numeric import D
from rbh_hedge_var.variational_gateway import VariationalGatewayError, VariationalOrderGateway


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


def _gw(monkeypatch, creds=True, scheme="token"):
    gw = VariationalOrderGateway(symbol="XAU", env_file=".env", cfg={"auth_scheme": scheme})
    if creds:
        gw._token = "t"
        gw._api_key = "k"
        gw._api_secret = "s"
    else:
        gw._token = ""
        gw._api_key = ""
        gw._api_secret = ""
    return gw


def test_submit_blocked_while_armed(monkeypatch):
    gw = _gw(monkeypatch)
    with pytest.raises(WriteBlockedError):
        gw.submit_market_order("sell", D("2.7"))


def test_missing_creds_fail_closed(monkeypatch):
    gw = _gw(monkeypatch, creds=False)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(VariationalGatewayError):
        gw.submit_market_order("sell", D("2.7"))


def test_submit_returns_acceptance_not_trusted_fill(monkeypatch):
    gw = _gw(monkeypatch)
    posts = []

    def fake_post(path, body):
        posts.append((path, body))
        if "indicative" in path:
            return {"price": "4330.5"}
        return {"rfq_id": "r1", "status": "accepted", "fill_price": "4330.4"}

    monkeypatch.setattr(gw, "_post", fake_post)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = gw.submit_market_order("sell", D("2.7"), symbol="XAU")
    assert out["rfq_id"] == "r1"
    assert out["ref_price"] == D("4330.5")
    # "accepted" is NOT a terminal fill -> must not be trusted as filled.
    assert out["terminal_ok"] is False
    assert len(posts) == 2  # indicative then order


def test_submit_rejected_raises(monkeypatch):
    gw = _gw(monkeypatch)

    def fake_post(path, body):
        if "indicative" in path:
            return {"price": "4330"}
        return {"rfq_id": "r1", "status": "rejected"}

    monkeypatch.setattr(gw, "_post", fake_post)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(VariationalGatewayError):
        gw.submit_market_order("sell", D("2.7"))


def test_signed_position_short_is_negative(monkeypatch):
    # Real /api/positions is a bare LIST; each row nests data under position_info
    # with instrument.underlying. Unsigned qty + side="short" -> negate.
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: [
        {"position_info": {"qty": "2.7", "side": "short",
                           "instrument": {"underlying": "XAU"}}}])
    assert gw.signed_position("XAU") == D("-2.7")


def test_signed_position_keeps_signed_qty(monkeypatch):
    # If qty already carries its sign, the side guard must be a no-op.
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: [
        {"position_info": {"qty": "-2.7", "instrument": {"underlying": "XAU"}}}])
    assert gw.signed_position("XAU") == D("-2.7")


def test_signed_position_flat_when_symbol_absent(monkeypatch):
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: [
        {"position_info": {"qty": "1.0", "instrument": {"underlying": "ETH"}}}])
    assert gw.signed_position("XAU") == D("0")


def test_signed_position_raises_on_unrecognized_row(monkeypatch):
    # Fail-CLOSED: a row missing position_info must RAISE, never read as flat.
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: [
        {"asset": "XAU", "net_quantity": "2.7"}])
    with pytest.raises(VariationalGatewayError):
        gw.signed_position("XAU")


def test_signed_position_raises_when_payload_not_list(monkeypatch):
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: {
        "positions": [{"asset": "XAU"}]})
    with pytest.raises(VariationalGatewayError):
        gw.signed_position("XAU")


def test_avg_entry_price_reads_row(monkeypatch):
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_get_authed", lambda path, params=None: [
        {"position_info": {"qty": "2.7", "side": "short", "avg_entry_price": "4331.2",
                           "instrument": {"underlying": "XAU"}}}])
    assert gw.avg_entry_price("XAU") == D("4331.2")


def test_token_auth_uses_vr_token_cookie(monkeypatch):
    gw = _gw(monkeypatch)
    h = gw._auth_headers("GET", "/api/positions", "")
    assert h == {"Cookie": "vr-token=t"}
    assert "Authorization" not in h  # Bearer is what caused 'No token'


def test_token_auth_strips_bearer_prefix(monkeypatch):
    gw = _gw(monkeypatch)
    gw._token = "  Bearer abc123  "  # operator pasted the prefix + whitespace
    assert gw._auth_headers("GET", "/api/positions", "") == {"Cookie": "vr-token=abc123"}


def test_hmac_scheme_still_signs(monkeypatch):
    gw = _gw(monkeypatch, scheme="hmac")
    h = gw._auth_headers("POST", "/api/orders/new/market", "{}")
    assert set(h) == {"X-API-KEY", "X-API-TIMESTAMP", "X-API-SIGNATURE"}
