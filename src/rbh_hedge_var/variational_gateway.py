"""Variational ORDER GATEWAY (Phase 2 — real orders, guarded).

Write counterpart to ``variational_client.VariationalReadOnlyClient``. Variational
is an RFQ venue: request a firm quote for a size, then submit a market order that
the venue fills against that quote. The returned ``rfq_id`` means the request was
ACCEPTED — never that it filled. Fill truth is proven downstream by the live
executor via private-position reconciliation (rbh-hedge-v2's hard lesson).

Two auth schemes (``variational.auth_scheme``):
  * "token" (DEFAULT) — the接法 verified live in variational-ondo: a browser
    session token (VARIATIONAL_TOKEN) sent as the ``vr-token`` COOKIE (NOT an
    Authorization: Bearer header — the omni frontend ignores Bearer and returns
    {"message":"No token"}; vo var_api.py:100-118) against the same
    omni.variational.io host + Cloudflare impersonation Phase 1 already uses.
    Paths default to the vo-verified endpoints.
  * "hmac" — API-key/secret HMAC-SHA256(ts+method+path+body). Kept for a future
    official API-key programme; UNVERIFIED, opt-in only.

SAFETY: every mutating call flows through ``http_util.request_json`` ->
``net_guard``, so while the guard is armed any POST raises before a socket opens.
Absent credentials fail closed with a clear error.

⚠️ REVIEW-REQUIRED: confirm the token endpoints + the order response field names
against the live vo client before disarming. Everything here is config-
overridable under ``variational.paths`` so paths can be pinned without code edits.
"""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import time
from decimal import Decimal
from typing import Any

from . import http_util, net_guard
from .config import read_env
from .numeric import ZERO, D

VAR_BASE_URL = "https://omni.variational.io"

# vo-verified token endpoints as the default; overridable per-scheme via config.
_DEFAULT_PATHS = {
    "indicative": "/api/quotes/indicative",
    "order": "/api/orders/new/market",
    "accept": "/api/quotes/accept",
    "positions": "/api/positions",        # vo var_api.py get_positions — real endpoint
    "portfolio": "/api/portfolio",        # account balance/margin (preflight can reuse)
}

# A returned order is ACCEPTED, not filled. Only these terminal states are worth
# reporting; fill quantity is still confirmed by position reconciliation, never
# trusted from the body. "accepted"/"pending" are deliberately NOT here.
_TERMINAL_OK = frozenset({"filled", "done", "complete", "completed"})


class VariationalGatewayError(RuntimeError):
    pass


class VariationalOrderGateway:
    def __init__(self, *, base_url: str = VAR_BASE_URL, symbol: str = "XAU",
                 env_file: str = ".env", cfg: dict[str, Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.symbol = symbol.upper()
        self.env_file = env_file
        vcfg = cfg or {}
        self.scheme = str(vcfg.get("auth_scheme", "token")).lower()
        self.paths = {**_DEFAULT_PATHS, **(vcfg.get("paths") or {})}
        self.impersonate = bool(vcfg.get("impersonate", True))  # Cloudflare
        # Instrument identity for the RFQ bodies. funding_interval_s is PART of
        # the instrument identity (XAU = 14400s, NOT the 3600 vo hardcodes); a
        # wrong value 400s with 'contract not found'. Injected from config
        # (engine feeds expected_variational_funding_interval_s) so it can be
        # pinned to the live-metadata value without a code edit.
        self._funding_interval_s = int(vcfg.get("funding_interval_s") or 14400)
        self._settlement_asset = str(vcfg.get("settlement_asset", "USDC"))
        self._instrument_type = str(vcfg.get("instrument_type", "perpetual_future"))
        # token scheme creds
        self._token = read_env(env_file, ("VARIATIONAL_TOKEN", "VARIATIONAL_API_TOKEN"))
        # hmac scheme creds
        self._api_key = read_env(env_file, ("VARIATIONAL_API_KEY",))
        self._api_secret = read_env(env_file, ("VARIATIONAL_API_SECRET",))

    # ---- auth --------------------------------------------------------------
    def _require_creds(self) -> None:
        if self.scheme == "token":
            if not self._token:
                raise VariationalGatewayError("VARIATIONAL_TOKEN missing — refuse to trade")
        elif not self._api_key or not self._api_secret:
            raise VariationalGatewayError("VARIATIONAL_API_KEY/SECRET missing — refuse to trade")

    def _auth_headers(self, method: str, path: str, body_str: str) -> dict[str, str]:
        if self.scheme == "token":
            # Variational's omni frontend authenticates via the `vr-token`
            # COOKIE, not an Authorization: Bearer header (vo var_api.py:100-118
            # tried Bearer, abandoned it — "Try cookie only"). Sending Bearer
            # gets {"message":"No token"}. Clean the token the same way vo does:
            # strip whitespace and any accidentally-copied "Bearer " prefix.
            token = (self._token or "").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            return {"Cookie": f"vr-token={token}"}
        ts = str(int(time.time() * 1000))
        prehash = f"{ts}{method.upper()}{path}{body_str}"
        sig = hmac.new(self._api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        return {"X-API-KEY": self._api_key, "X-API-TIMESTAMP": ts, "X-API-SIGNATURE": sig}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_creds()
        body_str = _json.dumps(body, separators=(",", ":"))
        headers = self._auth_headers("POST", path, body_str)
        res = http_util.request_json("POST", self.base_url + path, headers=headers,
                                     body=body, impersonate=self.impersonate)
        if res.status < 200 or res.status >= 300:
            raise VariationalGatewayError(f"POST {path} HTTP {res.status}: {res.text[:200]}")
        return res.json

    def _get_authed(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_creds()
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"
        headers = self._auth_headers("GET", path, "")
        res = http_util.request_json("GET", url, headers=headers, impersonate=self.impersonate)
        if res.status < 200 or res.status >= 300:
            raise VariationalGatewayError(f"GET {path} HTTP {res.status}: {res.text[:200]}")
        return res.json

    # ---- mutating surface (guarded) ---------------------------------------
    def _instrument(self, sym: str) -> dict[str, Any]:
        """The instrument identity object Variational's quote/order bodies wrap
        the request in (vo var_api.py:314-337). ``funding_interval_s`` is PART of
        the instrument identity — XAU's official listing is 14400s (not the 3600
        vo hardcodes for its assets); a wrong value yields a 'contract not found'
        400. It is injected from the operator's validated config
        (``expected_variational_funding_interval_s``), which the funding-unit gate
        already cross-checks against live metadata before any live unlock."""
        return {
            "underlying": sym,
            "funding_interval_s": int(self._funding_interval_s),
            "settlement_asset": self._settlement_asset,
            "instrument_type": self._instrument_type,
        }

    def request_quote(self, qty: Decimal, symbol: str | None = None) -> dict[str, Any]:
        """Two-step RFQ, step 1: request a firm quote for a size. The body wraps a
        nested ``instrument`` object and carries NO side (side is chosen at order
        time). Returns {"price", "quote_id"} — the quote_id threads into the order
        request. Raises on an unusable quote."""
        sym = (symbol or self.symbol).upper()
        q = self._post(self.paths["indicative"], {
            "instrument": self._instrument(sym),
            "qty": str(D(qty)),
        })
        price = _first_decimal(q, ("price", "quote_price", "indicative_price", "fill_price"))
        quote_id = q.get("quote_id") or q.get("id") or q.get("rfq_id")
        if price is None or price <= ZERO:
            raise VariationalGatewayError(f"unusable indicative quote: {q}")
        if quote_id is None:
            raise VariationalGatewayError(f"quote missing quote_id: {q}")
        return {"price": price, "quote_id": quote_id}

    def indicative_price(self, side: str, qty: Decimal, symbol: str | None = None) -> Decimal:
        """Back-compat price probe (side is ignored by the venue at quote time)."""
        return self.request_quote(qty, symbol)["price"]

    def submit_market_order(self, side: str, qty: Decimal, *, symbol: str | None = None,
                            reduce_only: bool = False,
                            max_slippage_pct: Decimal = D("0.005")) -> dict[str, Any]:
        """Submit a market order. Returns an ACCEPTANCE record (rfq_id + ref
        price) — NOT a proven fill. The caller MUST confirm the fill by position
        reconciliation. Raises if the guard is armed, creds are missing, or the
        venue rejects the order outright.

        Two-step RFQ: request ONE quote (price + quote_id), then submit the order
        referencing that quote_id. Field names are the vo-verified ones
        (``is_reduce_only``, ``max_slippage``) — NOT ``reduce_only``/
        ``max_slippage_pct`` (var_api.py:350-365)."""
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError(
                "write-guard armed: refusing Variational submit_market_order (net_guard.disarm to go live)")
        if side not in ("buy", "sell"):
            raise VariationalGatewayError(f"bad side {side!r}")
        sym = (symbol or self.symbol).upper()
        quote = self.request_quote(qty, sym)
        ref_price = quote["price"]
        resp = self._post(self.paths["order"], {
            "quote_id": quote["quote_id"],
            "side": side,
            "max_slippage": float(D(max_slippage_pct)),
            "is_reduce_only": bool(reduce_only),
        })
        rfq_id = resp.get("rfq_id") or resp.get("order_id") or resp.get("id")
        status = str(resp.get("status") or "").lower()
        if rfq_id is None or status in ("rejected", "cancelled", "canceled", "error", "failed"):
            raise VariationalGatewayError(f"order rejected: {resp}")
        return {
            "venue": "variational", "symbol": sym, "side": side,
            "rfq_id": rfq_id, "status": status, "ref_price": ref_price,
            "reported_fill_price": _first_decimal(resp, ("fill_price", "avg_price", "price")),
            "terminal_ok": status in _TERMINAL_OK,  # informational only; not trusted
        }

    # ---- reconciliation ----------------------------------------------------
    def _position_info(self, symbol: str) -> dict[str, Any] | None:
        """Nested position row for ``symbol`` from ``/api/positions``.

        The omni endpoint returns a bare LIST of rows; each row nests the real
        data under ``position_info`` (qty + instrument), verified against vo's
        executor.py::_position_qty_var. FAIL-CLOSED: if the payload is not a
        list, or a row is present but not in the expected shape, we RAISE rather
        than skip it — silently reading an unrecognized structure as "no
        position" is a dangerous fail-open that would make the single-leg
        watchdog think a live leg vanished. An empty list (genuinely flat) is
        fine and returns None."""
        sym = symbol.upper()
        data = self._get_authed(self.paths["positions"])
        if not isinstance(data, list):
            raise VariationalGatewayError(
                f"positions payload not a list: {type(data).__name__} {str(data)[:160]}")
        for row in data:
            if not isinstance(row, dict):
                raise VariationalGatewayError(f"unrecognized position row: {str(row)[:200]}")
            info = row.get("position_info")
            if not isinstance(info, dict):
                raise VariationalGatewayError(
                    f"position row missing position_info: {str(row)[:200]}")
            inst = info.get("instrument") or {}
            if str(inst.get("underlying") or inst.get("symbol") or "").upper() == sym:
                return info
        return None

    def signed_position(self, symbol: str | None = None) -> Decimal:
        info = self._position_info((symbol or self.symbol).upper())
        if info is None:
            return ZERO
        qty = _first_decimal(info, ("qty", "net_quantity", "quantity", "size")) or ZERO
        # Defensive sign: if qty is reported UNSIGNED but a side/direction field
        # marks a short/sell, force it negative. If qty already carries its sign
        # (vo accumulates signed qty), the qty>0 guard makes this a no-op.
        side = str(info.get("side") or info.get("direction")
                   or (info.get("instrument") or {}).get("side") or "").lower()
        if qty > ZERO and side in ("short", "sell"):
            qty = -qty
        return qty

    def avg_entry_price(self, symbol: str | None = None) -> Decimal | None:
        info = self._position_info((symbol or self.symbol).upper())
        if info is None:
            return None
        return _first_decimal(info, ("avg_entry_price", "avg_price", "entry_price",
                                     "average_price", "avg_execution_price", "avg_cost"))


def _first_decimal(row: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for k in keys:
        if row.get(k) is not None:
            return D(row.get(k))
    return None
