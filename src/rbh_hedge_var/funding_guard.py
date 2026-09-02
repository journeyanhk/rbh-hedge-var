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

from .numeric import D, ZERO

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


def normalize_hourly(rate: Any, interval_s: int | None) -> Decimal | None:
    """Convert a per-interval OR annualized funding rate to a per-hour rate.

    Returns None when the interval is unknown — callers must treat None as
    "cannot compute", never as zero.

    Unit heuristic (ported from variational-ondo, validated against live data):
    Variational's public metadata publishes funding as an ANNUALIZED figure
    (e.g. ~0.26 == 26% APR), whereas RHC Lighter publishes a small per-interval
    decimal (e.g. 0.000128). We disambiguate by magnitude: |rate| > 0.01 is
    treated as annualized and divided by periods-per-year first; otherwise the
    rate is already per-interval. This is the concrete resolution of the
    "4h vs 1h / annualized vs per-period"口径 risk flagged in the design review.
    """
    if interval_s is None or interval_s <= 0:
        return None
    val = D(rate)
    interval = Decimal(int(interval_s))
    periods_per_year = Decimal(365 * 24 * 60 * 60) / interval
    per_interval = val / periods_per_year if abs(val) > Decimal("0.01") else val
    return per_interval * SECONDS_PER_HOUR / interval


def verify_units(
    var_interval_s: int | None,
    lighter_interval_s: int | None,
    *,
    expected_var_s: int,
    expected_lighter_s: int,
) -> FundingUnitResult:
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
        reason="both intervals published and match config",
        var_interval_s=var_interval_s,
        lighter_interval_s=lighter_interval_s,
        live_allowed=True,
    )
