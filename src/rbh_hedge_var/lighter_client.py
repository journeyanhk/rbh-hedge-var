"""RHC Lighter read-only adapter (Phase 1).

Endpoints, chain id and integer-scaling rules are taken from the grid-bot-wg
`dev004-dy` adapter (signer_worker.py / market.js) and independently confirmed
by rbh-hedge-v2's lighter_rh.py. This is the "borrow the接入 knowledge, not the
code" step from the design doc:

  * base_url  https://api.rh.lighter.xyz
  * chain_id  466324
  * GET /api/v1/orderBookDetails  -> per-market mark/index/last + decimals + fees
  * GET /api/v1/funding-rates     -> per-market decimal funding rate + interval
  * GET /api/v1/orderBookOrders   -> depth (for VWAP exit pricing)
  * GET /api/v1/account           -> read-only account snapshot (by index)

Phase 2 will add a SignerClient (lighter-python) behind net_guard.disarm();
none of that lives here. place/cancel raise on purpose.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import http_util
from .numeric import ZERO, D

RHC_BASE_URL = "https://api.rh.lighter.xyz"
RHC_CHAIN_ID = 466324
OFFICIAL_FUNDING_INTERVAL_S = 3600  # Lighter documents hourly UTC settlement.


class LighterError(RuntimeError):
    pass


class LighterReadOnlyClient:
    def __init__(self, base_url: str = RHC_BASE_URL, chain_id: int = RHC_CHAIN_ID,
                 account_index: int | None = None) -> None:
        if base_url.rstrip("/") != RHC_BASE_URL or int(chain_id) != RHC_CHAIN_ID:
            raise LighterError("only RHC mainnet https://api.rh.lighter.xyz (chainId=466324) allowed")
        self.base_url = base_url.rstrip("/")
        self.chain_id = int(chain_id)
        self.account_index = account_index
        self._markets: dict[str, dict[str, Any]] = {}

    # ---- public market data ------------------------------------------------
    def load_markets(self) -> dict[str, dict[str, Any]]:
        res = http_util.get_json(self.base_url + "/api/v1/orderBookDetails")
        rows = res.json.get("order_book_details")
        if not isinstance(rows, list) or not rows:
            raise LighterError(f"orderBookDetails empty (HTTP {res.status})")
        markets: dict[str, dict[str, Any]] = {}
        for row in rows:
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            markets[sym] = row
        self._markets = markets
        return markets

    def _market_row(self, symbol: str) -> dict[str, Any]:
        sym = symbol.upper()
        if sym not in self._markets:
            self.load_markets()
        if sym not in self._markets:
            raise LighterError(f"Lighter market {sym} not found")
        return self._markets[sym]

    def public_contract(self, symbol: str) -> dict[str, Any]:
        """Return a normalized snapshot for one market (mirrors VO's contract dict)."""
        row = self._market_row(symbol)
        return {
            "venue": "lighter",
            "symbol": str(row.get("symbol", symbol)).upper(),
            "market_id": int(row["market_id"]) if row.get("market_id") is not None else None,
            "mark_price": D(row.get("mark_price")),
            "index_price": D(row.get("index_price")),
            "last_price": D(row.get("last_trade_price")),
            "size_decimals": int(row.get("supported_size_decimals", row.get("size_decimals", 0)) or 0),
            "price_decimals": int(row.get("supported_price_decimals", row.get("price_decimals", 0)) or 0),
            "min_base_amount": D(row.get("min_base_amount") or 0),
            "min_quote_amount": D(row.get("min_quote_amount") or 0),
            "maker_fee": D(row.get("maker_fee") or 0),
            "taker_fee": D(row.get("taker_fee") or 0),
            "multiplier": D(row.get("multiplier") or 1),
            "status": str(row.get("status") or "").lower(),
            "reduce_only": bool((row.get("market_config") or {}).get("force_reduce_only")),
        }

    def funding_rate(self, symbol: str) -> dict[str, Any]:
        """Return decimal funding rate + interval. interval may be None -> caller gates it."""
        res = http_util.get_json(self.base_url + "/api/v1/funding-rates")
        rates = res.json.get("funding_rates")
        if not isinstance(rates, list):
            raise LighterError(f"funding-rates missing (HTTP {res.status})")
        sym = symbol.upper()
        match = next(
            (r for r in rates
             if str(r.get("symbol", "")).upper() == sym and r.get("exchange") == "lighter"),
            None,
        )
        if match is None:
            match = next((r for r in rates if str(r.get("symbol", "")).upper() == sym), None)
        if match is None:
            raise LighterError(f"Lighter funding for {sym} not found")
        raw_interval = match.get("funding_interval_s") or match.get("funding_interval")
        interval = int(raw_interval) if raw_interval else None
        return {
            "venue": "lighter",
            "symbol": sym,
            "rate": D(match.get("rate")),
            "funding_interval_s": interval,          # None => UNVERIFIED unit
            "next_funding_time": match.get("next_funding_time"),
            "official_interval_s": OFFICIAL_FUNDING_INTERVAL_S,
        }

    def order_book(self, symbol: str, limit: int = 50) -> dict[str, list[tuple[Decimal, Decimal]]]:
        row = self._market_row(symbol)
        res = http_util.get_json(
            self.base_url + "/api/v1/orderBookOrders",
            params={"market_id": int(row["market_id"]), "limit": limit},
        )
        bids_raw = res.json.get("bids") or []
        asks_raw = res.json.get("asks") or []
        bids = [(D(r.get("price")), D(r.get("remaining_base_amount"))) for r in bids_raw]
        asks = [(D(r.get("price")), D(r.get("remaining_base_amount"))) for r in asks_raw]
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        return {"bids": bids, "asks": asks}

    def account_snapshot(self) -> dict[str, Any] | None:
        if self.account_index is None:
            return None
        res = http_util.get_json(
            self.base_url + "/api/v1/account",
            params={"by": "index", "value": str(self.account_index), "active_only": "true"},
        )
        accounts = res.json.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != 1:
            return None
        acct = accounts[0]
        positions = []
        for raw in acct.get("positions") or []:
            qty = D(raw.get("position")) * (D(1) if int(raw.get("sign", 1)) >= 0 else D(-1))
            if qty == ZERO:
                continue
            positions.append({
                "symbol": str(raw.get("symbol", "")).upper(),
                "qty": qty,
                "entry": D(raw.get("avg_entry_price")),
                "liquidation": D(raw.get("liquidation_price")),
            })
        return {
            "equity": D(acct.get("collateral")),
            "available": D(acct.get("available_balance")),
            "positions": positions,
        }

    # ---- private funding settlements (review4 P0-D attestation source) -----
    def funding_history(self, symbol: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Real funding-settlement history for the configured account.

        Feeds ``funding_attest.validate_settlements`` so the go-live gate can
        prove Lighter's hourly cadence empirically (Lighter never publishes
        ``funding_interval_s``). Returns [{"timestamp": <s>, "amount": <usdt>}].

        REVIEW-REQUIRED: the exact RHC endpoint/param shape must be confirmed
        against the live API before go-live. Parsing tolerates field-name
        variants and fails closed (raises on transport error; skips malformed
        rows) so a wrong shape yields NO attestation rather than a fake one."""
        if self.account_index is None:
            raise LighterError("account_index required for funding history")
        row = self._market_row(symbol)
        res = http_util.get_json(
            self.base_url + "/api/v1/fundings",
            params={"account_index": str(self.account_index),
                    "market_id": int(row["market_id"]), "limit": int(limit)},
        )
        raw = (res.json.get("fundings") or res.json.get("funding_payments")
               or res.json.get("payments") or res.json.get("funding_history") or [])
        out: list[dict[str, Any]] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            ts = (r.get("timestamp") or r.get("ts") or r.get("time")
                  or r.get("settled_at") or r.get("funding_timestamp"))
            if ts is None:
                continue
            ts = int(ts)
            if ts > 10_000_000_000:   # ms -> s
                ts //= 1000
            amt = (r.get("amount") if r.get("amount") is not None else
                   r.get("funding") if r.get("funding") is not None else
                   r.get("payment") if r.get("payment") is not None else
                   r.get("value") if r.get("value") is not None else 0)
            out.append({"timestamp": ts, "amount": D(amt)})
        return out

    # ---- write surface (blocked in Phase 1) --------------------------------
    def place_market_order(self, *args: Any, **kwargs: Any):
        raise LighterError("Phase 1 read-only client cannot place orders (Phase 2 feature)")

    def cancel_all_orders(self, *args: Any, **kwargs: Any):
        raise LighterError("Phase 1 read-only client cannot cancel orders (Phase 2 feature)")
