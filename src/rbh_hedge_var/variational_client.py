"""Variational read-only adapter (Phase 1).

Uses the public GUI metadata endpoint that variational-ondo already relies on:

    GET https://omni.variational.io/api/metadata/supported_assets?cex_asset=XAU

The response maps the symbol to a list of asset rows carrying price, funding
rate and (per VO) a funding_interval_s field. We read it unauthenticated via
curl_cffi Chrome impersonation (Cloudflare rejects plain urllib). If the fetch
fails or the interval is absent, the snapshot is returned with
funding_interval_s=None so the funding-unit guard fails closed rather than
guessing.

IMPORTANT (from rbh-hedge-v2): Variational public quotes are INDICATIVE, not a
firm fill. This client is data-only; Phase 2 order placement (RFQ mutation) is
a separate, guarded module.
"""
from __future__ import annotations

from typing import Any

from . import http_util
from .numeric import D

VAR_BASE_URL = "https://omni.variational.io"

# Candidate field names seen across VO code paths; we try them in order.
_PRICE_KEYS = ("price", "mark_price", "index_price", "last_price")
_RATE_KEYS = ("next_funding_rate", "funding_rate", "fundingRate", "predicted_funding_rate")
_INTERVAL_KEYS = ("funding_interval_s", "funding_interval", "fundingIntervalS")
_TICK_KEYS = ("min_qty_tick", "min_quantity", "qty_step", "min_qty")


class VariationalError(RuntimeError):
    pass


class VariationalReadOnlyClient:
    def __init__(self, base_url: str = VAR_BASE_URL, symbol: str = "XAU") -> None:
        self.base_url = base_url.rstrip("/")
        self.symbol = symbol.upper()

    def _first(self, row: dict[str, Any], keys: tuple[str, ...]):
        for k in keys:
            if row.get(k) is not None:
                return row.get(k)
        return None

    def asset(self, symbol: str | None = None) -> dict[str, Any]:
        sym = (symbol or self.symbol).upper()
        url = f"{self.base_url}/api/metadata/supported_assets?cex_asset={sym}"
        res = http_util.get_json(url, impersonate=True)
        if res.status != 200:
            raise VariationalError(f"variational metadata {sym} HTTP {res.status}: {res.text[:160]}")
        data = res.json
        rows = data.get(sym) or data.get(sym.lower()) or []
        if not rows:
            raise VariationalError(f"variational metadata {sym} empty")
        row = rows[0]
        price = self._first(row, _PRICE_KEYS)
        rate = self._first(row, _RATE_KEYS)
        interval = self._first(row, _INTERVAL_KEYS)
        tick = self._first(row, _TICK_KEYS)
        return {
            "venue": "variational",
            "symbol": sym,
            "price": D(price),
            "funding_rate": D(rate),
            # None when the endpoint omits it -> funding-unit guard fails closed.
            "funding_interval_s": int(interval) if interval else None,
            "min_qty_tick": D(tick) if tick else None,
            "raw": row,
            "received_at_ms": res.received_at_ms,
        }

    # ---- write surface (blocked in Phase 1) --------------------------------
    def place_order(self, *args: Any, **kwargs: Any):
        raise VariationalError("Phase 1 read-only client cannot place orders (Phase 2 feature)")
