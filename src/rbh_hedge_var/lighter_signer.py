"""RHC Lighter SIGNER client (Phase 2 — real orders, guarded).

This is the write counterpart to ``lighter_client.LighterReadOnlyClient``. It
wraps the official ``lighter-python`` SignerClient, which signs a zk transaction
locally and posts it to the sequencer. The接入 shape (market_index, scaled
integer base_amount/price, is_ask, reduce_only, client_order_index) is taken
from grid-bot-wg `dev004-dy` (signer_worker.py) — the same knowledge Phase 1's
read client borrowed, now on the write side.

SAFETY (the whole reason this file is small and paranoid):

  * The ``lighter`` SDK opens its OWN aiohttp session, so a create_order call
    does NOT flow through ``http_util`` / ``net_guard``. Therefore EVERY
    mutating method here asserts ``net_guard.is_armed() is False`` first. While
    the guard is armed (all of Phase 1, and Phase 2 until an operator explicitly
    disarms it) every order path raises before the SDK is even touched.
  * The SDK is imported LAZILY inside ``_signer()`` so importing this module —
    and running the whole Phase 1 shadow engine and its tests — never requires
    ``lighter`` to be installed. It is only needed the moment someone actually
    goes live.
  * Sizing is fail-closed: a missing size/price decimal raises rather than
    guessing a scale (a wrong scale is a 10x-notional order).

Reconciliation reuses the ALREADY-VERIFIED read-only ``/api/v1/account``
endpoint via the read client, so position truth does not depend on the signer.
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from . import net_guard
from .lighter_client import LighterReadOnlyClient
from .numeric import ZERO, D, quantize_down, to_int_scaled

LIVE_CONFIRM_TOKEN = "I_UNDERSTAND_LIVE_TRADING"


class LighterSignerError(RuntimeError):
    pass


def _client_order_index() -> int:
    """Unique-ish per-order client index (uint). Lighter dedupes on this."""
    return int(time.time() * 1000) % 2_000_000_000 + (uuid.uuid4().int % 1000)


class LighterSignerClient:
    """Places/cancels real orders on RHC Lighter. Read data still comes from the
    read-only client so there is a single source of market/account truth."""

    def __init__(self, *, base_url: str, chain_id: int, account_index: int,
                 api_key_private_key: str, api_key_index: int,
                 read_client: LighterReadOnlyClient | None = None,
                 signer_factory: Any | None = None) -> None:
        if not api_key_private_key:
            raise LighterSignerError("LIGHTER_API_KEY_PRIVATE_KEY missing — cannot sign")
        if account_index is None:
            raise LighterSignerError("LIGHTER_ACCOUNT_INDEX missing — cannot sign")
        self.base_url = base_url.rstrip("/")
        self.chain_id = int(chain_id)
        self.account_index = int(account_index)
        self._pk = api_key_private_key
        self._api_key_index = int(api_key_index)
        self.read = read_client or LighterReadOnlyClient(
            base_url=self.base_url, chain_id=self.chain_id, account_index=self.account_index)
        # signer_factory is a test seam: inject a fake SignerClient without the SDK.
        self._signer_factory = signer_factory
        self._signer_obj: Any | None = None

    # ---- lazy SDK ----------------------------------------------------------
    def _signer(self) -> Any:
        if self._signer_obj is not None:
            return self._signer_obj
        if self._signer_factory is not None:
            self._signer_obj = self._signer_factory()
            return self._signer_obj
        try:  # imported ONLY when going live; Phase 1 never reaches here
            from lighter import SignerClient  # type: ignore
        except Exception as exc:  # pragma: no cover - env dependent
            raise LighterSignerError(
                "lighter-python SDK not installed; `pip install lighter-python` to go live"
            ) from exc
        # SignerClient's constructor changed across SDK versions: older builds
        # take (url, private_key, account_index, api_key_index); newer builds
        # take (url, account_index, api_private_keys={index: pk}, chain_id).
        # Inspect the actual installed signature and build kwargs to match,
        # rather than guessing and crashing on an unexpected-keyword error.
        import inspect
        params = inspect.signature(SignerClient.__init__).parameters
        kwargs: dict[str, Any] = {"url": self.base_url, "account_index": self.account_index}
        if "api_private_keys" in params:
            kwargs["api_private_keys"] = {self._api_key_index: self._pk}
        elif "private_key" in params:
            kwargs["private_key"] = self._pk
            if "api_key_index" in params:
                kwargs["api_key_index"] = self._api_key_index
        else:
            raise LighterSignerError(
                "unrecognized lighter SignerClient signature "
                f"({list(params)}) — cannot pass the API private key")
        if "chain_id" in params:
            kwargs["chain_id"] = self.chain_id
        self._signer_obj = SignerClient(**kwargs)
        return self._signer_obj

    # ---- sizing (fail-closed) ---------------------------------------------
    def _market_meta(self, symbol: str) -> dict[str, Any]:
        c = self.read.public_contract(symbol)
        if c.get("market_id") is None or c.get("size_decimals") is None or c.get("price_decimals") is None:
            raise LighterSignerError(f"Lighter {symbol} missing market_id/decimals — refuse to size")
        return c

    def scaled_amounts(self, symbol: str, qty: Decimal, price: Decimal) -> dict[str, int]:
        c = self._market_meta(symbol)
        size_dec = int(c["size_decimals"])
        price_dec = int(c["price_decimals"])
        step = D(1).scaleb(-size_dec)
        q = quantize_down(D(qty), step)
        if q <= ZERO:
            raise LighterSignerError(f"qty {qty} rounds to zero at {size_dec} decimals")
        return {
            "market_index": int(c["market_id"]),
            "base_amount": to_int_scaled(q, size_dec, "down"),
            "price_scaled": to_int_scaled(D(price), price_dec, "nearest"),
            "size_decimals": size_dec,
            "price_decimals": price_dec,
        }

    # ---- mutating surface (guarded) ---------------------------------------
    def place_market_order(self, symbol: str, side: str, qty: Decimal, ref_price: Decimal,
                           *, reduce_only: bool = False,
                           slippage_pct: Decimal = D("0.002")) -> dict[str, Any]:
        """Immediate-or-cancel taker order. ``side`` is 'buy'|'sell'.

        A market order on Lighter is an aggressive limit at a slippage-protected
        price so the sequencer will not fill worse than ``ref_price*(1±slip)``.
        """
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError(
                "write-guard armed: refusing Lighter place_market_order (call net_guard.disarm to go live)")
        if side not in ("buy", "sell"):
            raise LighterSignerError(f"bad side {side!r}")
        is_ask = side == "sell"
        limit = D(ref_price) * (D(1) - slippage_pct) if is_ask else D(ref_price) * (D(1) + slippage_pct)
        amt = self.scaled_amounts(symbol, qty, limit)
        signer = self._signer()
        coi = _client_order_index()
        # SDK surface (lighter-python): create_market_order returns (tx, tx_hash, err).
        tx, tx_hash, err = _run(signer.create_market_order(
            market_index=amt["market_index"],
            client_order_index=coi,
            base_amount=amt["base_amount"],
            avg_execution_price=amt["price_scaled"],
            is_ask=is_ask,
            reduce_only=reduce_only,
        ))
        if err is not None:
            raise LighterSignerError(f"Lighter create_market_order failed: {err}")
        return {
            "venue": "lighter", "symbol": symbol.upper(), "side": side,
            "client_order_index": coi, "tx_hash": tx_hash,
            "base_amount": amt["base_amount"], "reduce_only": reduce_only,
        }

    def cancel_all(self, symbol: str | None = None) -> dict[str, Any]:
        if net_guard.is_armed():
            raise net_guard.WriteBlockedError("write-guard armed: refusing Lighter cancel_all")
        signer = self._signer()
        tx, tx_hash, err = _run(signer.cancel_all_orders())
        if err is not None:
            raise LighterSignerError(f"Lighter cancel_all failed: {err}")
        return {"venue": "lighter", "cancelled": True, "tx_hash": tx_hash}

    # ---- reconciliation (read-only) ---------------------------------------
    def signed_position(self, symbol: str) -> Decimal:
        """Signed base qty for one symbol (>0 long, <0 short, 0 flat)."""
        snap = self.read.account_snapshot()
        if not snap:
            return ZERO
        sym = symbol.upper()
        total = ZERO
        for p in snap.get("positions") or []:
            if str(p.get("symbol", "")).upper() == sym:
                total += D(p.get("qty"))
        return total

    def avg_entry_price(self, symbol: str) -> Decimal | None:
        """Real average entry price of the current position (for fill backfill).

        Used instead of the mark price so a live round's leg records the price it
        was actually filled at — this feeds both MTM and the close-out ledger."""
        snap = self.read.account_snapshot()
        if not snap:
            return None
        sym = symbol.upper()
        for p in snap.get("positions") or []:
            if str(p.get("symbol", "")).upper() == sym:
                entry = p.get("entry")
                return D(entry) if entry is not None and D(entry) > ZERO else None
        return None

    # ---- private-read auth (positionFunding etc.) --------------------------
    def auth_token(self) -> str:
        """Signed auth token for authenticated GET endpoints (e.g.
        /api/v1/positionFunding). Signs locally via the SDK — no network, no
        mutation, so the write-guard does not apply. Default ~10-min expiry."""
        import inspect
        signer = self._signer()
        make = signer.create_auth_token_with_expiry
        # Newer SDKs key auth by api_key_index (default 255); pass ours so the
        # token is signed with the key we actually loaded. Older SDKs omit it.
        if "api_key_index" in inspect.signature(make).parameters:
            auth, err = make(api_key_index=self._api_key_index)
        else:
            auth, err = make()
        if err is not None or not auth:
            raise LighterSignerError(f"Lighter auth-token creation failed: {err}")
        return auth


def _run(coro: Any) -> Any:
    """Run one SDK coroutine to completion.

    lighter-python is async. We keep the engine synchronous (one tick, one
    blocking call) rather than threading an event loop through the whole app.
    If the SDK method is synchronous (or a test fake returns a tuple directly),
    pass it through unchanged.
    """
    import asyncio
    import inspect
    if inspect.isawaitable(coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():  # pragma: no cover - defensive
                raise LighterSignerError("cannot run signer coroutine inside a running loop")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    return coro
