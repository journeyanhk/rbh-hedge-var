"""Variational ORDER GATEWAY (Phase 2 — real orders, guarded).

Write counterpart to ``variational_client.VariationalReadOnlyClient``. Variational
is an RFQ venue: request a firm quote for a size, then submit a market order that
the venue fills against that quote. The returned ``rfq_id`` means the request was
ACCEPTED — never that it filled. Fill truth is proven downstream by the live
executor via private-position reconciliation (rbh-hedge-v2's hard lesson).

Two auth schemes (``variational.auth_scheme``):
  * "token" (DEFAULT) — the接法 verified live in variational-ondo: a browser
    session Bearer token (VARIATIONAL_TOKEN) against the same omni.variational.io
    host + Cloudflare impersonation Phase 1 already uses. Paths default to the
    vo-verified endpoints.
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
    "positions": "/api/account/positions",
    # legacy hmac-scheme aliases (kept so an operator can pin either):
    "request_quote": "/api/rfq/quote",
    "accept_quote": "/api/rfq/accept",
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
            return {"Authorization": f"Bearer {self._token}"}
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
    def indicative_price(self, side: str, qty: Decimal, symbol: str | None = None) -> Decimal:
        sym = (symbol or self.symbol).upper()
        q = self._post(self.paths["indicative"], {"asset": sym, "side": side, "quantity": str(D(qty))})
        price = _first_decimal(q, ("price", "quote_price", "indicative_price", "fill_price"))
        if price is None or price <= ZERO:
            raise VariationalGatewayError(f"unusable indicative quote: {q}")
        return price

    def submit_market_order(self, side: str, qty: Decimal, *, symbol: str | None = None,
                            reduce_only: bool = False,
                            max_slippage_pct: Decimal = D("0.002")) -> dict[str, Any]:
        """Submit a market order. Returns an ACCEPTANCE record (rfq_id + ref
        price) — NOT a proven fill. The caller MUST confirm the fill by position
        reconciliation. Raises if the guard is armed, creds are missing, or the
        venue rejects the order outright."""
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError(
                "write-guard armed: refusing Variational submit_market_order (net_guard.disarm to go live)")
        if side not in ("buy", "sell"):
            raise VariationalGatewayError(f"bad side {side!r}")
        sym = (symbol or self.symbol).upper()
        ref_price = self.indicative_price(side, qty, sym)
        resp = self._post(self.paths["order"], {
            "asset": sym, "side": side, "quantity": str(D(qty)),
            "type": "market", "reduce_only": reduce_only,
            "max_slippage_pct": str(max_slippage_pct),
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
    def _position_row(self, symbol: str) -> dict[str, Any] | None:
        sym = symbol.upper()
        data = self._get_authed(self.paths["positions"])
        rows = data.get("positions") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None
        for r in rows:
            if str(r.get("asset") or r.get("symbol") or "").upper() == sym:
                return r
        return None

    def signed_position(self, symbol: str | None = None) -> Decimal:
        row = self._position_row((symbol or self.symbol).upper())
        if not row:
            return ZERO
        qty = _first_decimal(row, ("net_quantity", "position", "quantity", "size")) or ZERO
        if str(row.get("side") or "").lower() == "short" and qty > ZERO:
            qty = -qty
        return qty

    def avg_entry_price(self, symbol: str | None = None) -> Decimal | None:
        row = self._position_row((symbol or self.symbol).upper())
        if not row:
            return None
        return _first_decimal(row, ("avg_entry_price", "avg_price", "entry_price", "average_price"))


def _first_decimal(row: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for k in keys:
        if row.get(k) is not None:
            return D(row.get(k))
    return None
