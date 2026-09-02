"""LighterReadOnlyClient.funding_history — positionFunding auth + error surfacing."""
import json

import pytest

from rbh_hedge_var import http_util
from rbh_hedge_var.lighter_client import LighterError, LighterReadOnlyClient
from rbh_hedge_var.numeric import D


def _client():
    c = LighterReadOnlyClient(account_index=281474976710404)
    c._markets = {"XAU": {"symbol": "XAU", "market_id": 40}}  # skip load_markets network
    return c


def _res(status, payload):
    return http_util.HttpResult(status, json.dumps(payload), 1.0)


def test_funding_history_raises_on_auth_error(monkeypatch):
    """An 'auth required' body must raise, not be read as zero settlements."""
    def fake(method, url, *, headers=None, impersonate=False, timeout=12.0):
        return _res(401, {"code": 20001, "message": "auth required for main accounts"})
    monkeypatch.setattr(http_util, "request_json", fake)
    with pytest.raises(LighterError, match="auth required"):
        _client().funding_history("XAU", auth_token="tok")


def test_funding_history_raises_on_error_code_with_200_status(monkeypatch):
    def fake(method, url, *, headers=None, impersonate=False, timeout=12.0):
        return _res(200, {"code": 20001, "message": "invalid param"})
    monkeypatch.setattr(http_util, "request_json", fake)
    with pytest.raises(LighterError, match="invalid param"):
        _client().funding_history("XAU", auth_token="tok")


def test_funding_history_parses_position_fundings(monkeypatch):
    seen = {}

    def fake(method, url, *, headers=None, impersonate=False, timeout=12.0):
        seen["url"] = url
        seen["headers"] = headers
        return _res(200, {"code": 200, "position_fundings": [
            {"timestamp": 1788361200, "change": "0.03"},
            {"timestamp": 1788357600, "change": "0.02"},   # older, listed 2nd
        ]})
    monkeypatch.setattr(http_util, "request_json", fake)
    rows = _client().funding_history("XAU", auth_token="tok")
    assert [r["timestamp"] for r in rows] == [1788357600, 1788361200]  # sorted asc
    assert rows[0]["amount"] == D("0.02")
    assert seen["headers"] == {"authorization": "tok"}
    assert "account_index=281474976710404" in seen["url"]
    assert "market_ids=40" in seen["url"]
