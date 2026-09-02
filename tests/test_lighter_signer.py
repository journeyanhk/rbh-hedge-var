"""LighterSignerClient — guard enforcement, scaling, fill normalization."""
import pytest

from rbh_hedge_var import net_guard
from rbh_hedge_var.lighter_signer import LighterSignerClient, LighterSignerError
from rbh_hedge_var.net_guard import WriteBlockedError
from rbh_hedge_var.numeric import D


def setup_function():
    net_guard.arm()


def teardown_function():
    net_guard.arm()


class FakeRead:
    def __init__(self, positions=None):
        self._positions = positions or []

    def public_contract(self, symbol):
        return {"market_id": 40, "size_decimals": 4, "price_decimals": 2}

    def account_snapshot(self):
        return {"positions": self._positions}


class FakeSigner:
    def __init__(self):
        self.calls = []

    def create_market_order(self, **kw):
        self.calls.append(kw)
        return ({"tx": 1}, "0xabc", None)

    def cancel_all_orders(self):
        return ({}, "0xdef", None)


def _client(read=None, signer=None):
    return LighterSignerClient(
        base_url="https://api.rh.lighter.xyz", chain_id=466324, account_index=7,
        api_key_private_key="pk", api_key_index=0,
        read_client=read or FakeRead(), signer_factory=(lambda: signer) if signer else None)


def test_place_order_blocked_while_armed():
    c = _client(signer=FakeSigner())
    with pytest.raises(WriteBlockedError):
        c.place_market_order("XAU", "buy", D("2.7"), D("4320"))


def test_scaled_amounts_uses_decimals():
    c = _client()
    amt = c.scaled_amounts("XAU", D("2.76543"), D("4320.12"))
    # size_decimals=4 -> 2.7654 * 1e4 = 27654 (floored); price_decimals=2 -> 432012
    assert amt["base_amount"] == 27654
    assert amt["price_scaled"] == 432012
    assert amt["market_index"] == 40


def test_place_order_when_disarmed_calls_signer():
    signer = FakeSigner()
    c = _client(signer=signer)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = c.place_market_order("XAU", "buy", D("2.7"), D("4320"))
    assert out["venue"] == "lighter" and out["side"] == "buy"
    assert out["tx_hash"] == "0xabc"
    # buy is_ask False; slippage-protected limit above ref
    call = signer.calls[0]
    assert call["is_ask"] is False
    assert call["base_amount"] == 27000  # 2.7 * 1e4


def test_place_order_propagates_signer_error():
    class Bad(FakeSigner):
        def create_market_order(self, **kw):
            return (None, None, "insufficient margin")
    c = _client(signer=Bad())
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(LighterSignerError):
        c.place_market_order("XAU", "sell", D("2.7"), D("4320"))


def test_signed_position_sums_symbol():
    read = FakeRead(positions=[
        {"symbol": "XAU", "qty": D("2.7")},
        {"symbol": "XAU", "qty": D("-0.7")},
        {"symbol": "ETH", "qty": D("5")},
    ])
    c = _client(read=read)
    assert c.signed_position("XAU") == D("2.0")


def test_auth_token_passes_api_key_index():
    class Sig:
        def create_auth_token_with_expiry(self, deadline=-1, *, api_key_index=255):
            return (f"tok:{api_key_index}", None)
    c = _client(signer=Sig())
    assert c.auth_token() == "tok:0"


def test_auth_token_raises_on_error():
    class Sig:
        def create_auth_token_with_expiry(self, deadline=-1, *, api_key_index=255):
            return (None, "signer offline")
    c = _client(signer=Sig())
    with pytest.raises(LighterSignerError):
        c.auth_token()


def test_signer_construction_adapts_to_sdk_signature(monkeypatch):
    """Regression: the installed SignerClient takes api_private_keys={idx: pk},
    not private_key=... . _signer() must inspect the real signature and build
    matching kwargs instead of crashing on an unexpected keyword."""
    import sys
    import types

    seen = {}

    class NewStyleSigner:  # mirrors current lighter-python constructor
        def __init__(self, url, account_index, api_private_keys,
                     nonce_management_type=None, chain_id=None):
            seen.update(url=url, account_index=account_index,
                        api_private_keys=api_private_keys, chain_id=chain_id)

    fake_mod = types.ModuleType("lighter")
    fake_mod.SignerClient = NewStyleSigner
    monkeypatch.setitem(sys.modules, "lighter", fake_mod)

    # signer_factory=None so the real SDK-import path runs against our fake
    c = LighterSignerClient(
        base_url="https://api.rh.lighter.xyz", chain_id=466324, account_index=7,
        api_key_private_key="pk", api_key_index=3, read_client=FakeRead())
    c._signer()
    assert seen["account_index"] == 7
    assert seen["api_private_keys"] == {3: "pk"}
    assert seen["chain_id"] == 466324
