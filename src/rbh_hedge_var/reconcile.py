"""Real position reconciliation (Phase 2).

Replaces the Phase 1 ``Engine._live_positions`` stub (which returned None) with
the true signed base quantity held on each venue, read from:

  * Lighter  — the VERIFIED read-only ``/api/v1/account`` endpoint (already used
    in Phase 1) via ``LighterReadOnlyClient.account_snapshot``.
  * Variational — the authenticated positions endpoint via
    ``VariationalOrderGateway.signed_position``.

The result feeds ``watchdog.check_single_leg``: {venue: signed_qty}. A source
that FAILS must not be silently reported as flat (that would hide a naked leg),
so a fetch error raises ``ReconcileError`` and the caller treats reconciliation
as unavailable — fail closed, do not pretend balanced.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .numeric import ZERO, D


class ReconcileError(RuntimeError):
    pass


def reconcile_positions(lighter_symbol: str, *, lighter_read: Any, var_gateway: Any,
                        var_symbol: str | None = None) -> dict[str, Decimal]:
    """Return {"lighter": signed_qty, "variational": signed_qty}.

    Each venue is read with ITS OWN market symbol: Lighter trades ``XAU`` while
    Variational may trade a different listing (e.g. the ``XAUS`` swap). Passing
    one shared symbol to both would read the Variational position as 0 whenever
    its listing differs — an invisible naked leg (var-desgin5 §2). ``var_symbol``
    defaults to ``lighter_symbol`` for the single-listing case.

    Raises ReconcileError if EITHER venue cannot be read — an unknown leg is more
    dangerous than a known imbalance, so we refuse to guess.
    """
    lit_sym = lighter_symbol.upper()
    var_sym = (var_symbol or lighter_symbol).upper()

    try:
        snap = lighter_read.account_snapshot()
    except Exception as exc:
        raise ReconcileError(f"lighter account read failed: {exc}") from exc
    lit_qty = ZERO
    for p in (snap or {}).get("positions") or []:
        if str(p.get("symbol", "")).upper() == lit_sym:
            lit_qty += D(p.get("qty"))

    try:
        var_qty = D(var_gateway.signed_position(var_sym))
    except Exception as exc:
        raise ReconcileError(f"variational position read failed: {exc}") from exc

    return {"lighter": lit_qty, "variational": var_qty}


def positions_balanced(expected_legs: list[dict[str, Any]],
                       live: dict[str, Decimal],
                       tolerance: Decimal = D("0.0000001")) -> bool:
    """True iff every expected leg matches live signed qty within tolerance.

    Used by the go-live preflight to confirm a clean (flat) starting book.
    """
    for leg in expected_legs:
        want = D(leg["qty"]) * (D(1) if leg["side"] == "buy" else D(-1))
        have = D(live.get(leg["venue"], ZERO))
        if abs(have - want) > tolerance:
            return False
    return True
