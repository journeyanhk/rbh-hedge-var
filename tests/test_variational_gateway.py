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


def test_submit_two_step_schema_and_acceptance(monkeypatch):
    # review14: the quote body wraps a nested `instrument` identity object and
    # carries NO side; the order body threads the returned quote_id with the
    # vo-verified field names (side, max_slippage, is_reduce_only).
    gw = _gw(monkeypatch)
    posts = []

    def fake_post(path, body):
        posts.append((path, body))
        if "indicative" in path:
            return {"price": "4330.5", "quote_id": "q-42"}
        return {"rfq_id": "r1", "status": "accepted", "fill_price": "4330.4"}

    monkeypatch.setattr(gw, "_post", fake_post)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    out = gw.submit_market_order("sell", D("2.7"), symbol="XAU")
    assert out["rfq_id"] == "r1"
    assert out["ref_price"] == D("4330.5")
    # "accepted" is NOT a terminal fill -> must not be trusted as filled.
    assert out["terminal_ok"] is False
    assert len(posts) == 2  # indicative then order

    (qpath, qbody), (opath, obody) = posts
    # quote: nested instrument identity, string qty, NO side.
    assert "indicative" in qpath
    inst = qbody["instrument"]
    assert inst["underlying"] == "XAU"
    assert inst["funding_interval_s"] == 14400   # XAU listing, not vo's 3600
    assert inst["settlement_asset"] == "USDC"
    assert inst["instrument_type"] == "perpetual_rwa_future"  # XAU is an RWA perp
    assert inst["kind"] == "commodity"           # asset-class discriminator
    assert qbody["qty"] == "2.7"
    assert "side" not in qbody
    # order: threads quote_id + vo-verified field names.
    assert obody["quote_id"] == "q-42"
    assert obody["side"] == "sell"
    assert "max_slippage" in obody and "max_slippage_pct" not in obody
    assert "is_reduce_only" in obody and "reduce_only" not in obody


class _FakeReadClient:
    def __init__(self, raw):
        self._raw = raw

    def asset(self, sym=None):
        return {"raw": self._raw}


def test_instrument_identity_pulled_from_live_metadata(monkeypatch):
    # review16: the instrument_type/funding_interval/kind come from LIVE metadata,
    # not config guesses. XAU's real listing is a perpetual_rwa_future @ 14400s in
    # the commodity asset class; wrong values 400'd.
    read = _FakeReadClient({"instrument_type": "perpetual_rwa_future",
                            "funding_interval_s": 14400, "settlement_asset": "USDC",
                            "asset_class": "commodity"})
    gw = VariationalOrderGateway(symbol="XAU", env_file=".env",
                                 cfg={"auth_scheme": "token",
                                      # deliberately WRONG config fallbacks:
                                      "instrument_type": "perpetual_future",
                                      "asset_class": "equity",
                                      "funding_interval_s": 3600},
                                 read_client=read)
    inst = gw._instrument("XAU")
    assert inst["instrument_type"] == "perpetual_rwa_future"  # metadata wins
    assert inst["funding_interval_s"] == 14400                # metadata wins
    assert inst["kind"] == "commodity"                        # metadata wins


def test_instrument_falls_back_to_config_without_read_client():
    # No read_client (or a failing fetch) -> config fallbacks are used.
    gw = VariationalOrderGateway(symbol="XAU", env_file=".env",
                                 cfg={"auth_scheme": "token",
                                      "instrument_type": "perpetual_rwa_future",
                                      "asset_class": "commodity",
                                      "funding_interval_s": 14400})
    inst = gw._instrument("XAU")
    assert inst["instrument_type"] == "perpetual_rwa_future"
    assert inst["funding_interval_s"] == 14400
    assert inst["kind"] == "commodity"


def test_quote_missing_quote_id_raises(monkeypatch):
    # A quote without a usable quote_id can't thread into an order -> fail closed.
    gw = _gw(monkeypatch)
    monkeypatch.setattr(gw, "_post", lambda path, body: {"price": "4330"})
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(VariationalGatewayError):
        gw.submit_market_order("sell", D("2.7"))


def test_submit_rejected_raises(monkeypatch):
    gw = _gw(monkeypatch)

    def fake_post(path, body):
        if "indicative" in path:
            return {"price": "4330", "quote_id": "q-1"}
        return {"rfq_id": "r1", "status": "rejected"}

    monkeypatch.setattr(gw, "_post", fake_post)
    net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")
    with pytest.raises(VariationalGatewayError):
        gw.submit_market_order("sell", D("2.7"))


def test_funding_interval_from_cfg_flows_into_instrument(monkeypatch):
    # The instrument's funding_interval_s is injected from config, not hardcoded.
    gw = VariationalOrderGateway(symbol="XAU", env_file=".env",
                                 cfg={"auth_scheme": "token", "funding_interval_s": 7200})
    gw._token = "t"
    captured = {}

    def fake_post(path, body):
        captured[path] = body
        return {"price": "1", "quote_id": "q"}

    monkeypatch.setattr(gw, "_post", fake_post)
    gw.request_quote(D("1"), "XAU")
    inst = next(iter(captured.values()))["instrument"]
    assert inst["funding_interval_s"] == 7200


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
