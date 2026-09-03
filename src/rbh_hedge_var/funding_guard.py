"""Funding-interval unit hard gate.

This is the single most important economic check in the whole system. The
design analysis showed the net funding edge differs by >5x depending on
whether Variational quotes a 1h or a 4h funding rate. If we normalize with the
wrong interval we can turn a losing trade into an apparent winner.

Contract:
  * Both legs MUST publish a positive funding_interval_s to be VERIFIED.
  * If a leg's interval is missing -> UNVERIFIED (unit unknown).
  * If a leg's interval differs from the operator's expected config value ->
    MISMATCH (someone's assumption is stale; refuse live).

Only a VERIFIED result whose intervals match config is allowed to leave shadow
mode for live execution. In shadow mode we still compute economics but stamp
the snapshot so the dashboard and logs scream when the unit is not proven.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .numeric import ZERO, D

SECONDS_PER_HOUR = Decimal(3600)


@dataclass(frozen=True)
class FundingUnitResult:
    status: str            # "verified" | "unverified" | "mismatch"
    reason: str
    var_interval_s: int | None
    lighter_interval_s: int | None
    live_allowed: bool

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def heuristic_is_annualized(rate: Any) -> bool:
    """Magnitude heuristic: |rate| > 0.01 looks annualized, not per-interval."""
    return abs(D(rate)) > Decimal("0.01")


def normalize_hourly(rate: Any, interval_s: int | None,
                     unit_hint: str | None = None) -> Decimal | None:
    """Convert a per-interval OR annualized funding rate to a per-hour rate.

    Returns None when the interval is unknown — callers must treat None as
    "cannot compute", never as zero.

    Unit resolution (P1-2 fix): the venue's unit is taken from ``unit_hint``
    when provided ("annualized" | "per_interval"/"decimal"). Only when no hint
    is configured do we fall back to the magnitude heuristic. This removes the
    ambiguity where a rare 1.5%-per-interval rate could be misread as annualized.

    Validated live (2026-09-02): Variational publishes an ANNUALIZED figure over
    a 14400s (4h) interval; RHC Lighter publishes a small per-interval decimal.
    """
    if interval_s is None or interval_s <= 0:
        return None
    val = D(rate)
    interval = Decimal(int(interval_s))
    periods_per_year = Decimal(365 * 24 * 60 * 60) / interval
    hint = (unit_hint or "").strip().lower()
    if hint == "annualized":
        annualized = True
    elif hint in ("per_interval", "decimal", "per_period"):
        annualized = False
    else:
        annualized = heuristic_is_annualized(val)
    per_interval = val / periods_per_year if annualized else val
    return per_interval * SECONDS_PER_HOUR / interval


def unit_hint_conflicts(rate: Any, unit_hint: str | None) -> bool:
    """True when a configured unit disagrees with the magnitude heuristic.

    Used to raise a dashboard/log warning (not a hard block) so a misconfigured
    unit or an extreme market print is surfaced rather than silently trusted.
    """
    if not unit_hint:
        return False
    if D(rate) == ZERO:
        # A zero rate (e.g. Variational funding momentarily flat) trips the
        # magnitude heuristic toward "per_interval" and would spuriously warn on
        # an 'annualized' venue. Zero carries no unit information — never warn.
        return False
    hint = unit_hint.strip().lower()
    heuristic = heuristic_is_annualized(rate)
    if hint == "annualized":
        return not heuristic
    if hint in ("per_interval", "decimal", "per_period"):
        return heuristic
    return False


def verify_units(
    var_interval_s: int | None,
    lighter_interval_s: int | None,
    *,
    expected_var_s: int,
    expected_lighter_s: int,
    lighter_attested_interval_s: int | None = None,
) -> FundingUnitResult:
    # review4 P0-D: RHC Lighter never publishes an interval, which would weld the
    # live gate shut. A valid private funding attestation (proven cadence from
    # real settlements, see funding_attest) may supply the interval in its place.
    attested_used = False
    if lighter_interval_s is None and lighter_attested_interval_s:
        try:
            lighter_interval_s = int(lighter_attested_interval_s)
            attested_used = True
        except (TypeError, ValueError):
            lighter_interval_s = None
    if var_interval_s is None or lighter_interval_s is None:
        missing = []
        if var_interval_s is None:
            missing.append("variational")
        if lighter_interval_s is None:
            missing.append("lighter")
        return FundingUnitResult(
            status="unverified",
            reason=f"funding_interval_s missing for: {', '.join(missing)}",
            var_interval_s=var_interval_s,
            lighter_interval_s=lighter_interval_s,
            live_allowed=False,
        )
    if var_interval_s <= 0 or lighter_interval_s <= 0:
        return FundingUnitResult(
            status="unverified",
            reason="non-positive funding interval",
            var_interval_s=var_interval_s,
            lighter_interval_s=lighter_interval_s,
            live_allowed=False,
        )
    mismatches = []
    if int(var_interval_s) != int(expected_var_s):
        mismatches.append(f"variational {var_interval_s}s != expected {expected_var_s}s")
    if int(lighter_interval_s) != int(expected_lighter_s):
        mismatches.append(f"lighter {lighter_interval_s}s != expected {expected_lighter_s}s")
    if mismatches:
        return FundingUnitResult(
            status="mismatch",
            reason="; ".join(mismatches),
            var_interval_s=var_interval_s,
            lighter_interval_s=lighter_interval_s,
            live_allowed=False,
        )
    return FundingUnitResult(
        status="verified",
        reason=("both intervals match config"
                + (" (lighter via funding attestation)" if attested_used else
                   "; both intervals published")),
        var_interval_s=var_interval_s,
        lighter_interval_s=lighter_interval_s,
        live_allowed=True,
    )
