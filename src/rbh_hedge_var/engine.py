"""Engine — one tick wires data -> guard -> strategy -> state machine -> shadow.

Phase 1 guarantees:
  * net_guard stays ARMED; no order is ever sent.
  * live_trading is refused unless config.live_trading is true AND the funding
    unit is VERIFIED — and even then Phase 1 has no live executor, so it stays
    in shadow. The gate is here so Phase 2 only has to flip one branch.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import economics, net_guard, strategy, watchdog
from .config import env_int, read_env
from .funding_guard import normalize_hourly
from .lighter_client import LighterReadOnlyClient
from .numeric import D, ZERO, fmt
from .shadow_executor import ShadowExecutor
from .state_machine import StateMachine, IDLE, ENTERING, HOLDING, EXITING, COOLDOWN
from .variational_client import VariationalReadOnlyClient


class Engine:
    def __init__(self, cfg: dict[str, Any]) -> None:
        net_guard.arm()  # belt-and-suspenders: always read-only in Phase 1
        self.cfg = cfg
        vcfg = cfg.get("variational", {})
        lcfg = cfg.get("lighter", {})
        self.var = VariationalReadOnlyClient(
            base_url=vcfg.get("base_url", "https://omni.variational.io"),
            symbol=vcfg.get("symbol", "XAU"),
        )
        acct_index = env_int(lcfg.get("account_env_file", ".env"), ("LIGHTER_ACCOUNT_INDEX",))
        self.lighter = LighterReadOnlyClient(
            base_url=lcfg.get("base_url", "https://api.rh.lighter.xyz"),
            chain_id=int(lcfg.get("chain_id", 466324)),
            account_index=acct_index,
        )
        self.lighter_symbol = lcfg.get("symbol", "XAUT")
        self.executor = ShadowExecutor(cfg)
        self.sm = StateMachine(cfg.get("state_file", "state.json"))
        self.notional = D(cfg.get("notional_per_leg_usdt", 12000))
        self.last_snapshot: dict[str, Any] = {}
        self.last_error: str | None = None

    # ---- data --------------------------------------------------------------
    def fetch_snapshot(self) -> dict[str, Any]:
        data_errors: dict[str, str] = {}
        try:
            var_asset = self.var.asset()
        except Exception as exc:
            data_errors["variational"] = f"{type(exc).__name__}: {exc}"
            # Fail closed: no price, unpublished interval -> live stays blocked.
            var_asset = {"symbol": self.cfg.get("variational", {}).get("symbol", "XAU"),
                         "price": ZERO, "funding_rate": ZERO, "funding_interval_s": None}
        try:
            lit_contract = self.lighter.public_contract(self.lighter_symbol)
            lit_funding = self.lighter.funding_rate(self.lighter_symbol)
        except Exception as exc:
            data_errors["lighter"] = f"{type(exc).__name__}: {exc}"
            lit_contract = {"symbol": self.lighter_symbol, "mark_price": ZERO, "status": None}
            lit_funding = {"rate": ZERO, "funding_interval_s": None, "official_interval_s": 3600}
        snap = strategy.market_snapshot(var_asset, lit_contract, lit_funding, self.cfg)
        snap["data_errors"] = data_errors

        # economics overlay
        direction = strategy.choose_direction(snap.get("spread_hourly"))
        net_hr = economics.net_hourly_funding_usdt(
            direction or "", self.notional,
            snap.get("var_funding_hourly"), snap.get("lighter_funding_hourly"),
        )
        basis_gain = economics.entry_basis_gain_usdt(direction or "", self.notional, snap.get("basis"))
        rt_cost = economics.roundtrip_cost_usdt(self.notional, self.cfg)
        be_hours = economics.break_even_hours(rt_cost, basis_gain, net_hr)
        snap.update({
            "candidate_direction": direction,
            "net_funding_hourly_usdt": net_hr,
            "entry_basis_gain_usdt": basis_gain,
            "roundtrip_cost_usdt": rt_cost,
            "break_even_hours": be_hours,
            "notional_per_leg_usdt": self.notional,
        })
        return snap

    # ---- tick --------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        try:
            snap = self.fetch_snapshot()
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self.last_error, "mode": self.sm.mode}
        self.last_snapshot = snap

        # drawdown breaker first
        dd = watchdog.check_drawdown(D(self.sm.today_pnl()), D(self.cfg.get("max_daily_loss_usdt", 15)))
        if not dd.ok:
            self._log(f"[HALT] {dd.reason}")
            return {"ok": True, "mode": self.sm.mode, "halt": dd.reason, "snapshot": _display(snap)}

        mode = self.sm.mode
        action = "none"

        if mode == COOLDOWN:
            if self.sm.maybe_leave_cooldown():
                action = "cooldown_elapsed->idle"

        mode = self.sm.mode
        if mode == IDLE:
            should, direction, reason = strategy.entry_signal(snap, self.cfg)
            if should:
                action = self._do_entry(direction, reason, snap)
            else:
                action = f"no_entry:{reason}"

        elif mode == HOLDING:
            direction = self.sm.state.get("direction") or ""
            reversed_now = strategy.is_spread_reversed(snap, direction)
            streak = self.sm.bump_reversal(reversed_now)
            should_exit, reason = strategy.exit_signal(snap, direction, self.cfg, streak - 1 if reversed_now else 0)
            if not should_exit:
                tp, tp_reason = strategy.take_profit_signal(0.0, self.cfg)
                should_exit, reason = tp, tp_reason
            if should_exit:
                action = self._do_exit(reason, snap)
            else:
                action = "holding"

        self.sm.state["shadow"] = True
        self.sm.save()
        return {"ok": True, "mode": self.sm.mode, "action": action, "snapshot": _display(snap)}

    def _do_entry(self, direction: str, reason: str, snap: dict[str, Any]) -> str:
        # Live gate — Phase 1 always resolves to shadow.
        live = bool(self.cfg.get("live_trading")) and bool(snap.get("live_allowed_by_units"))
        if live and self.cfg.get("require_funding_unit_verified_for_live", True) and not snap.get("funding_verified"):
            live = False
        self.sm.begin_entry(direction, reason)
        book = None
        try:
            book = self.lighter.order_book(self.lighter_symbol)
        except Exception:
            book = None
        lit_contract = self.lighter.public_contract(self.lighter_symbol)
        size_step = D(1).scaleb(-int(lit_contract.get("price_decimals", 4)))  # placeholder if size unknown
        try:
            size_step = D(1).scaleb(-int(lit_contract.get("size_decimals", 4)))
        except Exception:
            pass
        result = self.executor.open_hedge(
            direction, self.notional,
            snap.get("var_price") or ZERO, snap.get("lighter_price") or ZERO,
            size_step, book,
        )
        if result.get("both_filled"):
            self.sm.confirm_hold(result["legs"])
            self._log(f"[SHADOW OPEN] {direction} {reason} | be_hours={fmt(snap.get('break_even_hours'),2)}")
            return f"shadow_open:{direction}"
        self.sm.abort_entry("leg_not_filled")
        return "entry_aborted"

    def _do_exit(self, reason: str, snap: dict[str, Any]) -> str:
        self.sm.begin_exit(reason)
        legs = self.sm.state.get("legs") or []
        book = None
        try:
            book = self.lighter.order_book(self.lighter_symbol)
        except Exception:
            book = None
        result = self.executor.close_hedge(
            legs, snap.get("var_price") or ZERO, snap.get("lighter_price") or ZERO, book,
        )
        pnl = float(result.get("price_pnl") or 0)
        self.sm.finish_exit(pnl, reason, int(self.cfg.get("close_cooldown_seconds", 1200)))
        self._log(f"[SHADOW CLOSE] {reason} | price_pnl={fmt(pnl,4)}")
        return f"shadow_close:{reason}"

    def _log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            p = Path(self.cfg.get("log_file", "logs/rbh_hedge_var.log"))
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _display(snap: dict[str, Any]) -> dict[str, Any]:
    """Decimal/None -> json-friendly for the monitor & one-shot output."""
    out = {}
    for k, v in snap.items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif k == "raw":
            continue
        else:
            out[k] = v
    return out
