"""Strategy: market snapshot, AUTO direction selection, entry/exit signals.

Ported and corrected from variational-ondo/strategy.py. Two deliberate changes
requested in the design review:

  1. Direction is AUTO — the side is derived from the sign of the hourly funding
     spread every tick, not a hard-coded whitelist. Short the higher-funding
     leg, long the lower-funding leg.
  2. Funding is normalized to hourly using the VERIFIED interval from
     funding_guard; if the unit is unverified the spread is flagged and
     live entry is refused upstream.

Direction vocabulary:
  * "short_var_long_lighter"  -> spread > 0 (Variational funding higher)
  * "short_lighter_long_var"  -> spread < 0 (Lighter funding higher)
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from . import funding_guard
from .numeric import ZERO, D


def _is_swap_leg(leg_cfg: dict[str, Any] | None, expected_interval_s: Any) -> bool:
    """A leg is a zero-funding SWAP when the operator declares it so: either
    funding_unit == "none" or the expected funding interval is 0. Never inferred
    from a momentarily-zero published rate (that carries no unit information)."""
    unit = str((leg_cfg or {}).get("funding_unit") or "").strip().lower()
    if unit == "none":
        return True
    try:
        return int(expected_interval_s) == 0
    except (TypeError, ValueError):
        return False


def market_snapshot(var_asset: dict[str, Any], lighter_contract: dict[str, Any],
                    lighter_funding: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    px_var = D(var_asset.get("price"))
    px_lit = lighter_contract.get("mark_price") or lighter_contract.get("last_price") or ZERO
    px_lit = D(px_lit)

    var_int = var_asset.get("funding_interval_s")            # published, may be None
    lit_int = lighter_funding.get("funding_interval_s")      # published, may be None

    # SWAP detection (var-desgin5): a Variational "Swap" market (e.g. XAUS) has
    # NO funding — rate=0, interval=0/absent. The operator declares it via config
    # (funding_unit="none" or expected_interval=0), NOT from a momentarily-zero
    # published rate. A swap leg contributes 0 to the funding spread and is exempt
    # from the interval gate.
    var_is_swap = _is_swap_leg(cfg.get("variational"),
                               cfg.get("expected_variational_funding_interval_s"))
    lit_is_swap = _is_swap_leg(cfg.get("lighter"),
                               cfg.get("expected_lighter_funding_interval_s"))

    # Live gate uses ONLY published intervals -> fails closed when unknown.
    # review4 P0-D: a valid private funding attestation may stand in for the
    # interval Lighter never publishes (engine injects it as a private cfg key).
    unit = funding_guard.verify_units(
        var_int, lit_int,
        expected_var_s=int(cfg.get("expected_variational_funding_interval_s", 3600)),
        expected_lighter_s=int(cfg.get("expected_lighter_funding_interval_s", 3600)),
        lighter_attested_interval_s=cfg.get("_attested_lighter_interval_s"),
        var_is_swap=var_is_swap,
        lighter_is_swap=lit_is_swap,
    )

    # Economics/display use a REFERENCE interval so the dashboard still shows
    # numbers when a venue omits the field (RHC funding-rates does). This is
    # explicitly a reference projection, never proof of settlement.
    var_ref = var_int or int(cfg.get("expected_variational_funding_interval_s", 3600))
    lit_ref = lit_int or int(lighter_funding.get("official_interval_s") or
                             cfg.get("expected_lighter_funding_interval_s", 3600))
    var_unit = (cfg.get("variational") or {}).get("funding_unit")
    lit_unit = (cfg.get("lighter") or {}).get("funding_unit")
    # Rate-BASIS period: the quoted rate applies over this many seconds, which
    # can DIFFER from the settlement interval. RHC Lighter's /funding-rates quotes
    # an 8h-basis rate (28800s) but SETTLES hourly (3600s) — proven from real
    # positionFunding rows: quoted 0.000032 / 8 = settled 0.000004. Economics must
    # normalize a per_interval rate over its BASIS, not the settlement interval,
    # or Lighter's hourly edge is overstated 8x. Variational's ANNUALIZED figure
    # is basis-independent (the interval cancels in normalize_hourly), so the
    # override only matters for Lighter; default to the reference interval.
    var_basis = int((cfg.get("variational") or {}).get("funding_rate_basis_s") or var_ref)
    lit_basis = int((cfg.get("lighter") or {}).get("funding_rate_basis_s") or lit_ref)
    var_hourly = (ZERO if var_is_swap
                  else funding_guard.normalize_hourly(var_asset.get("funding_rate"), var_basis, var_unit))
    lit_hourly = (ZERO if lit_is_swap
                  else funding_guard.normalize_hourly(lighter_funding.get("rate"), lit_basis, lit_unit))
    unit_warnings = []
    if not var_is_swap and funding_guard.unit_hint_conflicts(var_asset.get("funding_rate"), var_unit):
        unit_warnings.append(f"variational funding magnitude conflicts with configured unit '{var_unit}'")
    if not lit_is_swap and funding_guard.unit_hint_conflicts(lighter_funding.get("rate"), lit_unit):
        unit_warnings.append(f"lighter funding magnitude conflicts with configured unit '{lit_unit}'")
    spread_hourly = None
    if var_hourly is not None and lit_hourly is not None:
        spread_hourly = var_hourly - lit_hourly

    basis = (px_var / px_lit - D(1)) if (px_var > ZERO and px_lit > ZERO) else ZERO
    price_diff_abs = abs(px_var - px_lit) if (px_var > ZERO and px_lit > ZERO) else None

    return {
        "var_symbol": var_asset.get("symbol"),
        "lighter_symbol": lighter_contract.get("symbol"),
        "var_price": px_var,
        "lighter_price": px_lit,
        "var_funding_interval_s": var_int,
        "lighter_funding_interval_s": lit_int,
        "var_funding_interval_published": var_int is not None,
        "lighter_funding_interval_published": lit_int is not None,
        "funding_reference_interval_s": {"variational": var_ref, "lighter": lit_ref},
        "var_funding_hourly": var_hourly,
        "lighter_funding_hourly": lit_hourly,
        "var_is_swap": var_is_swap,
        "lighter_is_swap": lit_is_swap,
        "spread_hourly": spread_hourly,
        "basis": basis,
        "price_diff_abs": price_diff_abs,
        "funding_unit_status": unit.status,
        "funding_unit_reason": unit.reason,
        "funding_verified": unit.verified,
        "live_allowed_by_units": unit.live_allowed,
        "funding_unit_warnings": unit_warnings,
        "lighter_status": lighter_contract.get("status"),
        "lighter_reduce_only": lighter_contract.get("reduce_only"),
    }


def choose_direction(spread_hourly: Decimal | None) -> str | None:
    if spread_hourly is None or spread_hourly == ZERO:
        return None
    return "short_var_long_lighter" if spread_hourly > ZERO else "short_lighter_long_var"


def entry_signal(snap: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str, str]:
    """Return (should_enter, direction, reason)."""
    if snap.get("spread_hourly") is None:
        return False, "", "funding_unit_unknown:cannot_compute_spread"

    # var-desgin6: TradFi trading-hours gate. Never OPEN a Variational round when
    # its market (XAUS) is closed or so near a close that the round could not be
    # flattened before the RFQ freezes. Only active when trading_hours.enabled.
    if snap.get("var_session_enabled"):
        if not snap.get("var_market_open"):
            return False, "", "var_market_closed"
        to_close = snap.get("var_seconds_to_close")
        hours_cfg = (cfg.get("variational") or {}).get("trading_hours") or {}
        lead = hours_cfg.get("entry_lead_seconds")
        if lead is None:
            max_hold_h = float(cfg.get("max_hold_hours", 0) or 0)
            margin = float(hours_cfg.get("entry_margin", 1.2) or 1.2)
            lead = max_hold_h * 3600 * margin if max_hold_h > 0 else 90 * 60
        if to_close is not None and to_close < float(lead):
            return False, "", f"var_market_closing_in_{int(to_close)}s"

    if str(snap.get("lighter_status")) not in ("active", "None", "none", ""):
        if snap.get("lighter_status") and snap["lighter_status"] != "active":
            return False, "", f"lighter_market_{snap['lighter_status']}"
    if snap.get("lighter_reduce_only"):
        return False, "", "lighter_force_reduce_only"

    var_price = snap.get("var_price") or ZERO
    lit_price = snap.get("lighter_price") or ZERO
    if var_price <= ZERO or lit_price <= ZERO:
        return False, "", "invalid_market_price"

    price_diff = snap.get("price_diff_abs")
    max_diff = D(cfg.get("max_entry_price_diff_usdt", 0) or 0)
    if max_diff > ZERO and price_diff is not None and price_diff > max_diff:
        return False, "", f"price_diff_too_wide {price_diff}U > {max_diff}U"

    basis = abs(snap.get("basis") or ZERO)
    if basis > D(cfg.get("max_basis_pct", 0.015)):
        return False, "", f"basis_too_wide {basis}"

    spread = snap["spread_hourly"]
    threshold = D(cfg.get("entry_spread_threshold_hourly", 0) or 0)
    if abs(spread) <= ZERO:
        return False, "", "spread_zero"
    if abs(spread) < threshold:
        return False, "", f"spread_too_small {spread}/h < {threshold}/h"

    direction = choose_direction(spread)
    if direction is None:
        return False, "", "no_direction"

    mode = str(cfg.get("direction_mode", "auto")).lower()
    if mode != "auto":
        # Explicit lock: only enter if the auto side matches the locked side.
        if mode != direction:
            return False, "", f"direction_locked_{mode}_but_signal_{direction}"

    # review12: after the 8x funding correction the XAU carry is thin; single-
    # round PnL is dominated by basis moves. Optionally require the basis to be
    # in our favour at entry (short the richer leg) by at least a floor, so we
    # lock in a cushion that covers most of the round-trip cost and turns "small
    # loss" into "roughly flat". entry_basis_gain_usdt is computed upstream in
    # the economics overlay for the chosen direction (0 when basis is adverse).
    if cfg.get("require_entry_basis_gain", False):
        gain = D(snap.get("entry_basis_gain_usdt") or 0)
        floor = D(cfg.get("min_entry_basis_gain_usdt", 0) or 0)
        if gain < floor:
            return False, "", f"basis_gain_too_small {gain}U < {floor}U"

    if direction == "short_var_long_lighter":
        return True, direction, "short higher-funding Variational, long Lighter"
    return True, direction, "short higher-funding Lighter, long Variational"


def exit_signal(snap: dict[str, Any], direction: str, cfg: dict[str, Any],
                reversal_streak: int) -> tuple[bool, str]:
    """Trigger-based exit — never a timer.

    Priority: hard basis stop, then confirmed funding-spread reversal.

    `reversal_streak` is the count INCLUDING the current tick (the engine bumps
    it before calling this). We compare directly against the confirm threshold
    so there is only one place that owns the counting semantics.
    """
    basis = abs(snap.get("basis") or ZERO)
    if basis > D(cfg.get("force_exit_basis_pct", 0.02)):
        return True, f"basis_force_exit {basis}"

    if cfg.get("exit_on_spread_reversal", True):
        spread = snap.get("spread_hourly")
        if spread is not None:
            # Reversal = the edge we entered on has gone to/through zero.
            reversed_now = (
                (direction == "short_var_long_lighter" and spread <= ZERO)
                or (direction == "short_lighter_long_var" and spread >= ZERO)
            )
            need = int(cfg.get("spread_reversal_confirm_ticks", 3))
            if reversed_now and reversal_streak >= need:
                return True, f"funding_spread_reversal confirmed x{reversal_streak}"
    return False, ""


def take_profit_signal(total_pnl: Any, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Fire when the marked-to-market round PnL clears the take-profit threshold.

    review19 (止盈实亏 incident 21:43): the MTM total can print a small positive
    (+0.23) that is NOT actually realizable once the real close crosses the
    spread — the round then books a loss (−0.51). We require the MTM to clear the
    threshold by an extra ``take_profit_realizable_buffer_usdt`` so take-profit
    only fires when the gain is robust to exit-cost/MTM error. Buffer defaults to
    0 (legacy behaviour) and is disabled when <= 0.
    """
    threshold = D(cfg.get("take_profit_total_pnl_usdt", 0) or 0)
    buffer = D(cfg.get("take_profit_realizable_buffer_usdt", 0) or 0)
    if buffer < ZERO:
        buffer = ZERO
    pnl = D(total_pnl)
    trigger = threshold + buffer
    if threshold > ZERO and pnl >= trigger:
        if buffer > ZERO:
            return True, f"take_profit {pnl} >= {threshold}+{buffer} (realizable)"
        return True, f"take_profit {pnl} >= {threshold}"
    return False, ""


def round_stop_loss_signal(total_pnl: Any, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Per-round stop: total (price + funding) PnL below -max_round_loss_usdt.

    Consumes `max_round_loss_usdt`. Disabled when the config value is <= 0.
    """
    limit = D(cfg.get("max_round_loss_usdt", 0) or 0)
    if limit <= ZERO:
        return False, ""
    pnl = D(total_pnl)
    if pnl <= -limit:
        return True, f"round_stop_loss {pnl} <= -{limit}"
    return False, ""


def max_hold_exit_signal(opened_at: Any, cfg: dict[str, Any],
                         *, now: float | None = None) -> tuple[bool, str]:
    """VALIDATION-ONLY time exit. The strategy's real exits are trigger-based
    (never a timer); this optional guard forces a round to complete the whole
    open -> confirm -> accrue -> close -> reconcile -> ledger lifecycle within
    ``max_hold_hours`` so the BACK HALF of the pipeline gets exercised even when
    no price/funding trigger fires during a quiet validation window. It sits LAST
    in the exit-priority chain, so a genuine take-profit/stop-loss/reversal still
    wins. Set ``max_hold_hours`` to 0 (the default) to disable it and restore the
    pure trigger-based discipline for the standby regime."""
    hours = float(cfg.get("max_hold_hours", 0) or 0)
    if hours <= 0 or not opened_at:
        return False, ""
    current = time.time() if now is None else now
    held_h = (current - float(opened_at)) / 3600.0
    if held_h >= hours:
        return True, f"max_hold_elapsed {held_h:.1f}h >= {hours}h"
    return False, ""


def is_spread_reversed(snap: dict[str, Any], direction: str) -> bool:
    spread = snap.get("spread_hourly")
    if spread is None:
        return False
    if direction == "short_var_long_lighter":
        return spread <= ZERO
    if direction == "short_lighter_long_var":
        return spread >= ZERO
    return False
