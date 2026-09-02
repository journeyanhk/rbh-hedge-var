"""Variational ORDER GATEWAY (Phase 2 — real orders, guarded).

Write counterpart to ``variational_client.VariationalReadOnlyClient``. Variational
is an RFQ venue: you request a firm quote for a size, then accept it within its
validity window. Public metadata quotes are INDICATIVE (rbh-hedge-v2's warning);
the firm price only exists inside an authenticated RFQ, so live entry MUST go
through this gateway, never the public price.

SAFETY:
  * Every mutating call flows through ``http_util.request_json`` -> ``net_guard``,
    so while the guard is armed any POST raises before a socket opens.
  * Authentication material (API key/secret) is read from the env file lazily;
    absent credentials fail closed with a clear error rather than sending an
    unsigned request.

⚠️ REVIEW-REQUIRED before go-live: the exact REST paths and the request-signing
scheme below are the documented-but-UNVERIFIED shape. They are intentionally
config-overridable (``variational.paths`` / ``variational.auth_scheme``) so the
reviewer can pin them to Variational's real private API without touching code.
The signing helper implements the common HMAC-SHA256(timestamp+method+path+body)
scheme; confirm against the venue docs and adjust ``_sign`` if it differs.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any

from . import http_util, net_guard
from .config import read_env
from .numeric import ZERO, D

VAR_BASE_URL = "https://omni.variational.io"

_DEFAULT_PATHS = {
    "request_quote": "/api/rfq/quote",
    "accept_quote": "/api/rfq/accept",
    "positions": "/api/account/positions",
}


class VariationalGatewayError(RuntimeError):
    pass


class VariationalOrderGateway:
    def __init__(self, *, base_url: str = VAR_BASE_URL, symbol: str = "XAU",
                 env_file: str = ".env", cfg: dict[str, Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.symbol = symbol.upper()
        self.env_file = env_file
        vcfg = cfg or {}
        self.paths = {**_DEFAULT_PATHS, **(vcfg.get("paths") or {})}
        self.impersonate = bool(vcfg.get("impersonate", True))  # Cloudflare
        self._api_key = read_env(env_file, ("VARIATIONAL_API_KEY",))
        self._api_secret = read_env(env_file, ("VARIATIONAL_API_SECRET",))

    # ---- auth --------------------------------------------------------------
    def _require_creds(self) -> None:
        if not self._api_key or not self._api_secret:
            raise VariationalGatewayError(
                "VARIATIONAL_API_KEY / VARIATIONAL_API_SECRET missing — refuse to trade")

    def _sign(self, method: str, path: str, body_str: str) -> dict[str, str]:
        """HMAC-SHA256(timestamp + method + path + body). REVIEW-REQUIRED."""
        ts = str(int(time.time() * 1000))
        prehash = f"{ts}{method.upper()}{path}{body_str}"
        sig = hmac.new(self._api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        return {
            "X-API-KEY": self._api_key,
            "X-API-TIMESTAMP": ts,
            "X-API-SIGNATURE": sig,
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_creds()
        import json as _json
        body_str = _json.dumps(body, separators=(",", ":"))
        headers = self._sign("POST", path, body_str)
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
        headers = self._sign("GET", path, "")
        res = http_util.request_json("GET", url, headers=headers, impersonate=self.impersonate)
        if res.status < 200 or res.status >= 300:
            raise VariationalGatewayError(f"GET {path} HTTP {res.status}: {res.text[:200]}")
        return res.json

    # ---- mutating surface (guarded) ---------------------------------------
    def place_taker_order(self, side: str, qty: Decimal, *, symbol: str | None = None,
                          reduce_only: bool = False, max_slippage_pct: Decimal = D("0.002")) -> dict[str, Any]:
        """RFQ taker fill: request a firm quote, then accept it if within bounds.

        Returns a normalized fill dict {venue, symbol, side, filled_qty,
        filled_price, order_id}. Raises if the guard is armed, creds are missing,
        or the quote moved beyond ``max_slippage_pct`` from its indicative price.
        """
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError(
                "write-guard armed: refusing Variational place_taker_order (net_guard.disarm to go live)")
        if side not in ("buy", "sell"):
            raise VariationalGatewayError(f"bad side {side!r}")
        sym = (symbol or self.symbol).upper()

        quote = self._post(self.paths["request_quote"], {
            "asset": sym, "side": side, "quantity": str(D(qty)),
            "type": "taker", "reduce_only": reduce_only,
        })
        quote_id = quote.get("quote_id") or quote.get("id")
        quote_price = _first_decimal(quote, ("price", "quote_price", "fill_price"))
        if quote_id is None or quote_price is None or quote_price <= ZERO:
            raise VariationalGatewayError(f"unusable quote: {quote}")

        accept = self._post(self.paths["accept_quote"], {
            "quote_id": quote_id, "max_slippage_pct": str(max_slippage_pct),
        })
        status = str(accept.get("status") or "").lower()
        filled_qty = _first_decimal(accept, ("filled_quantity", "filled_qty", "quantity")) or ZERO
        filled_price = _first_decimal(accept, ("fill_price", "avg_price", "price")) or quote_price
        if status not in ("filled", "accepted", "done") or filled_qty <= ZERO:
            raise VariationalGatewayError(f"quote not filled: {accept}")
        return {
            "venue": "variational", "symbol": sym, "side": side,
            "filled_qty": filled_qty, "filled_price": filled_price,
            "order_id": accept.get("order_id") or quote_id,
        }

    # ---- reconciliation ----------------------------------------------------
    def signed_position(self, symbol: str | None = None) -> Decimal:
        sym = (symbol or self.symbol).upper()
        data = self._get_authed(self.paths["positions"])
        rows = data.get("positions") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return ZERO
        total = ZERO
        for r in rows:
            if str(r.get("asset") or r.get("symbol") or "").upper() != sym:
                continue
            qty = _first_decimal(r, ("net_quantity", "position", "quantity", "size")) or ZERO
            side = str(r.get("side") or "").lower()
            if side == "short" and qty > ZERO:
                qty = -qty
            total += qty
        return total


def _first_decimal(row: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for k in keys:
        if row.get(k) is not None:
            return D(row.get(k))
    return None
