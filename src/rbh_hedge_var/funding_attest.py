"""Funding-settlement attestation (review4 P0-D).

The funding-unit gate fails closed when a venue omits ``funding_interval_s``.
RHC Lighter NEVER publishes it, so the public path can never reach VERIFIED and
the go-live door is welded shut. This module provides the missing evidence-based
unlock: pull real private funding SETTLEMENTS, prove the cadence and magnitude
empirically, and write a time-boxed attestation that ``funding_guard`` accepts in
lieu of a published interval.

Pure validation (``validate_settlements``) is separated from IO so it is unit-
testable with synthetic rows. The attestation is fail-closed: it expires (default
7 days) and carries the observed interval so a stale or wrong-cadence settlement
stream cannot silently keep live enabled.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .numeric import ZERO, D

DEFAULT_VALIDITY_S = 7 * 24 * 3600


def validate_settlements(rows: list[dict[str, Any]], *, expected_interval_s: int,
                         cadence_tolerance_pct: float = 0.2,
                         min_samples: int = 3) -> dict[str, Any]:
    """Validate a private funding-settlement stream.

    rows: newest-or-oldest-first list of {timestamp (s), amount (usdt)} — order
    agnostic, we sort. Checks:
      * at least ``min_samples`` settlements,
      * consecutive gaps ≈ expected_interval_s within tolerance (cadence proof),
    Returns {ok, observed_interval_s, samples, reason}.
    """
    ts = sorted(int(r.get("timestamp") or r.get("ts") or 0) for r in rows if r)
    ts = [t for t in ts if t > 0]
    if len(ts) < min_samples:
        return {"ok": False, "observed_interval_s": None, "samples": len(ts),
                "reason": f"need >= {min_samples} settlements, got {len(ts)}"}
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return {"ok": False, "observed_interval_s": None, "samples": len(ts),
                "reason": "no positive gaps between settlements"}
    median = sorted(gaps)[len(gaps) // 2]
    lo = expected_interval_s * (1 - cadence_tolerance_pct)
    hi = expected_interval_s * (1 + cadence_tolerance_pct)
    if not (lo <= median <= hi):
        return {"ok": False, "observed_interval_s": median, "samples": len(ts),
                "reason": f"median gap {median}s outside {lo:.0f}-{hi:.0f}s "
                          f"(expected {expected_interval_s}s)"}
    return {"ok": True, "observed_interval_s": int(median), "samples": len(ts),
            "reason": f"cadence proven: median {median}s over {len(ts)} settlements"}


def build_attestation(venue: str, observed_interval_s: int, *, samples: int,
                      detail: str, validity_s: int = DEFAULT_VALIDITY_S,
                      now: int | None = None) -> dict[str, Any]:
    now = int(now if now is not None else time.time())
    return {
        "venue": venue,
        "interval_s": int(observed_interval_s),
        "samples": int(samples),
        "detail": detail,
        "at": now,
        "expires_at": now + int(validity_s),
    }


def valid_attestation(att: dict[str, Any] | None, venue: str,
                      now: int | None = None) -> dict[str, Any] | None:
    """Return the attestation iff it is for ``venue`` and unexpired, else None."""
    if not isinstance(att, dict):
        return None
    if att.get("venue") != venue:
        return None
    if int(att.get("interval_s") or 0) <= 0:
        return None
    now = int(now if now is not None else time.time())
    if int(att.get("expires_at") or 0) <= now:
        return None
    return att


def amount_plausibility(rows: list[dict[str, Any]], *, rate: Decimal | None,
                        notional: Decimal, interval_s: int) -> str:
    """Best-effort magnitude sanity note (not a hard gate): compare a settlement
    amount to rate*notional over one interval. Returned as human-readable detail
    since private amount signs/units vary by venue."""
    if rate is None or notional <= ZERO:
        return "amount check skipped (rate/notional unknown)"
    amounts = [abs(D(r.get("amount") or r.get("funding") or 0)) for r in rows if r]
    amounts = [a for a in amounts if a > ZERO]
    if not amounts:
        return "amount check skipped (no settlement amounts)"
    observed = sorted(amounts)[len(amounts) // 2]
    expected = abs(D(rate)) * D(notional)
    if expected <= ZERO:
        return "amount check skipped (expected 0)"
    ratio = observed / expected
    return f"median settlement {observed} vs expected/interval {expected} (ratio {ratio:.2f})"
