"""Engine — one tick wires data -> guard -> strategy -> state machine -> shadow.

Phase 1 guarantees:
  * net_guard stays ARMED; no order is ever sent.
  * live_trading is refused unless config.live_trading is true AND the funding
    unit is VERIFIED — and even then Phase 1 has no live executor, so it stays
    in shadow. The gate is here so Phase 2 only has to flip one branch.

Review fixes wired here (see var-review1.md):
  * P0-1 funding accrual every HOLDING tick, split from price PnL at close.
  * P0-2 mark-to-market each HOLDING tick -> take-profit + per-round stop-loss.
  * P0-3 ENTERING/EXITING recovery branches so a crash-restart cannot deadlock.
  * P0-4 single-leg watchdog wired + Telegram alerts for HALT / single-leg /
    data-source failures.
  * P1-1 leg symbols from config; P1-3 fail-closed size step; P1-4 streak passed
    directly; P1-5 HALT latched, persisted and alerted (manual clear required).
"""
from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import economics, net_guard, strategy, watchdog
from .config import env_int
from .lighter_client import LighterReadOnlyClient
from .numeric import ZERO, D, fmt
from .shadow_executor import ShadowExecutor
from .state_machine import COOLDOWN, ENTERING, EXITING, HOLDING, IDLE, StateMachine
from .tg import TelegramNotifier
from .variational_client import VariationalReadOnlyClient

SECONDS_PER_HOUR = D(3600)


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
        self.var_symbol = vcfg.get("symbol", "XAU")
        self.lighter_symbol = lcfg.get("symbol", "XAU")   # P1-1: RHC market is XAU
        self.executor = ShadowExecutor(cfg)
        self.sm = StateMachine(cfg.get("state_file", "state.json"))
        self.tg = TelegramNotifier(cfg)
        self.notional = D(cfg.get("notional_per_leg_usdt", 12000))
        self.last_snapshot: dict[str, Any] = {}
        self.last_error: str | None = None
        self._data_fail_streak = 0

    # ---- data --------------------------------------------------------------
    def fetch_snapshot(self) -> dict[str, Any]:
        data_errors: dict[str, str] = {}
        try:
            var_asset = self.var.asset()
        except Exception as exc:
            data_errors["variational"] = f"{type(exc).__name__}: {exc}"
            # Fail closed: no price, unpublished interval -> live stays blocked.
            var_asset = {"symbol": self.var_symbol,
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
        # 0) Latched HALT (P1-5): once tripped, refuse all activity until a human
        #    clears state["halt"]. Persisted, so it survives restart.
        if self.sm.is_halted():
            return {"ok": True, "mode": self.sm.mode, "halt": self.sm.halt_reason(),
                    "snapshot": _display(self.last_snapshot)}

        # 1) Crash-restart recovery (P0-3): the engine only ever loads a
        #    persisted ENTERING/EXITING if it died mid-transition. Resolve it
        #    before doing anything else so the machine can never deadlock.
        if self.sm.mode == ENTERING:
            self.sm.abort_entry("recovered_after_restart")
            self._log("[RECOVERY] ENTERING found on boot -> abort_entry (shadow)")
        elif self.sm.mode == EXITING:
            self._recover_exit()

        try:
            snap = self.fetch_snapshot()
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._note_data_failure(self.last_error)
            return {"ok": False, "error": self.last_error, "mode": self.sm.mode}
        self.last_snapshot = snap

        # Track data-source health (P0-4 alert on sustained failure).
        if snap.get("data_errors"):
            self._note_data_failure("; ".join(f"{k}:{v}" for k, v in snap["data_errors"].items()))
        else:
            self._data_fail_streak = 0

        # 2) Drawdown circuit breaker -> latch HALT (P1-5).
        dd = watchdog.check_drawdown(D(self.sm.today_pnl()), D(self.cfg.get("max_daily_loss_usdt", 15)))
        if not dd.ok:
            if self.sm.set_halt(dd.reason):
                self._log(f"[HALT] {dd.reason}")
                self._alert(f"🛑 HALT (drawdown): {dd.reason}. Manual clear of state.halt required.")
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
            action = self._hold_tick(snap)

        self.sm.state["shadow"] = True
        self.sm.save()
        return {"ok": True, "mode": self.sm.mode, "action": action, "snapshot": _display(snap)}

    # ---- holding: accrual + MTM + watchdog + exit --------------------------
    def _hold_tick(self, snap: dict[str, Any]) -> str:
        direction = self.sm.state.get("direction") or ""
        legs = self.sm.state.get("legs") or []
        book = self._safe_book()

        # P0-1/P0-5: accrue funding for the elapsed wall-clock time, SIGNED by the
        # held direction so a reversal correctly accrues a NEGATIVE increment
        # (we pay funding) instead of the unsigned entry-candidate edge.
        held_hr = economics.held_hourly_funding_usdt(
            direction, self.notional,
            snap.get("var_funding_hourly"), snap.get("lighter_funding_hourly"),
        )
        now = int(time.time())
        last = self.sm.state.get("funding_last_accrual_ts") or self.sm.state.get("opened_at") or now
        elapsed = max(0, now - int(last))
        if held_hr is not None and elapsed > 0:
            inc = D(held_hr) * (D(elapsed) / SECONDS_PER_HOUR)
            self.sm.accrue_funding(float(inc))
        elif held_hr is None and elapsed > 0:
            # Funding unknown (missing rate): we conservatively accrue nothing but
            # still advance the clock. Log it so a long data gap is visible.
            self._log(f"[FUNDING] rate unavailable for {elapsed}s -> accrued 0 (ledger may understate)")
        self.sm.state["funding_last_accrual_ts"] = now
        snap["held_funding_hourly_usdt"] = held_hr
        funding_pnl = D(self.sm.funding_accrued())

        # P0-2: mark-to-market the price legs, combine with funding.
        price_pnl = self.executor.mark_to_market(
            legs, snap.get("var_price") or ZERO, snap.get("lighter_price") or ZERO, book,
        )
        total_pnl = price_pnl + funding_pnl
        snap["unrealized_price_pnl_usdt"] = price_pnl
        snap["funding_accrued_usdt"] = funding_pnl
        snap["unrealized_total_pnl_usdt"] = total_pnl

        # P0-4: single-leg watchdog. Phase 1 "reality" is the shadow legs (always
        # balanced) so live_positions is None -> OK; the wiring is live for P2.
        live_positions = self._live_positions()
        wd = watchdog.check_single_leg(legs, live_positions)
        snap["watchdog_action"] = wd.action
        if not wd.ok:
            self._log(f"[WATCHDOG] {wd.action}: {wd.reason}")
            self._alert(f"⚠️ Watchdog {wd.action}: {wd.reason}")
            self._do_exit("watchdog_" + wd.action, snap)
            return f"watchdog_exit:{wd.action}"

        # Exit priority: basis/reversal -> take-profit -> per-round stop-loss.
        reversed_now = strategy.is_spread_reversed(snap, direction)
        streak = self.sm.bump_reversal(reversed_now)            # P1-4: pass directly
        should_exit, reason = strategy.exit_signal(snap, direction, self.cfg, streak)
        if not should_exit:
            should_exit, reason = strategy.take_profit_signal(total_pnl, self.cfg)
        if not should_exit:
            should_exit, reason = strategy.round_stop_loss_signal(total_pnl, self.cfg)
        if should_exit:
            return self._do_exit(reason, snap)
        return "holding"

    # ---- entry / exit ------------------------------------------------------
    def _do_entry(self, direction: str, reason: str, snap: dict[str, Any]) -> str:
        # Live gate — Phase 1 always resolves to shadow.
        live = bool(self.cfg.get("live_trading")) and bool(snap.get("live_allowed_by_units"))
        if live and self.cfg.get("require_funding_unit_verified_for_live", True) and not snap.get("funding_verified"):
            live = False

        # P1-3: size step MUST come from size_decimals; refuse entry otherwise.
        lit_contract = self.lighter.public_contract(self.lighter_symbol)
        size_decimals = lit_contract.get("size_decimals")
        if size_decimals is None:
            self._log("[ENTRY BLOCKED] lighter size_decimals unknown -> fail closed")
            return "entry_blocked:no_size_decimals"
        try:
            size_step = D(1).scaleb(-int(size_decimals))
        except Exception:
            self._log(f"[ENTRY BLOCKED] bad size_decimals={size_decimals!r}")
            return "entry_blocked:bad_size_decimals"

        self.sm.begin_entry(direction, reason)
        book = self._safe_book()
        result = self.executor.open_hedge(
            direction, self.notional,
            snap.get("var_price") or ZERO, snap.get("lighter_price") or ZERO,
            size_step, book,
            var_symbol=self.var_symbol, lit_symbol=self.lighter_symbol,
        )
        if result.get("both_filled"):
            self.sm.confirm_hold(result["legs"])
            self._log(f"[SHADOW OPEN] {direction} {reason} | be_hours={fmt(snap.get('break_even_hours'),2)}")
            return f"shadow_open:{direction}"
        self.sm.abort_entry("leg_not_filled")
        return "entry_aborted"

    def _do_exit(self, reason: str, snap: dict[str, Any]) -> str:
        self.sm.begin_exit(reason)
        return self._close_and_finish(reason, snap)

    def _close_and_finish(self, reason: str, snap: dict[str, Any]) -> str:
        """Close both legs and book the round. Assumes state is already EXITING
        (either via begin_exit or a recovered restart), so it never re-transitions
        into EXITING — that would be an illegal self-transition (P0-3 fix).

        P1-9: refuse to settle on a zero/absent price. On a crash-restart the
        recovery snapshot can be empty, which would mark the long leg at
        (0 - entry) * qty — a huge fake loss written to the ledger that could
        even trip the drawdown HALT. Fail closed: stay in EXITING, log, and let
        the next tick retry once real prices are back."""
        var_price = snap.get("var_price") or ZERO
        lit_price = snap.get("lighter_price") or ZERO
        if var_price <= ZERO or lit_price <= ZERO:
            self._log(f"[RECOVERY DEFERRED] exit '{reason}' held: "
                      f"var_price={var_price} lit_price={lit_price} — retry next tick")
            return f"exit_deferred:{reason}"
        legs = self.sm.state.get("legs") or []
        book = self._safe_book()
        result = self.executor.close_hedge(legs, var_price, lit_price, book)
        price_pnl = float(result.get("price_pnl") or 0)
        funding_pnl = self.sm.funding_accrued()   # P0-1: booked separately
        self.sm.finish_exit(price_pnl, funding_pnl, reason,
                            int(self.cfg.get("close_cooldown_seconds", 1200)))
        self._log(f"[SHADOW CLOSE] {reason} | price_pnl={fmt(price_pnl,4)} "
                  f"funding_pnl={fmt(funding_pnl,4)} total={fmt(price_pnl + funding_pnl,4)}")
        return f"shadow_close:{reason}"

    def _recover_exit(self) -> None:
        """P0-3: resume an interrupted exit. State is already EXITING on boot, so
        run the close directly without begin_exit. Phase 1 = re-run shadow close."""
        self._log("[RECOVERY] EXITING found on boot -> re-run shadow exit")
        try:
            snap = self.fetch_snapshot()
        except Exception:
            snap = self.last_snapshot or {}
        self._close_and_finish("recovered_exit", snap)

    # ---- helpers -----------------------------------------------------------
    def _safe_book(self) -> dict[str, Any] | None:
        try:
            return self.lighter.order_book(self.lighter_symbol)
        except Exception:
            return None

    def _live_positions(self) -> dict[str, Decimal] | None:
        """Phase 1: no live reconciliation source -> None (watchdog returns OK).

        Phase 2 replaces this with real signed positions from both venues.
        """
        return None

    def _note_data_failure(self, detail: str) -> None:
        self._data_fail_streak += 1
        threshold = int(self.cfg.get("data_failure_alert_streak", 5))
        if self._data_fail_streak == threshold:
            self._log(f"[DATA] {self._data_fail_streak} consecutive failures: {detail}")
            self._alert(f"📉 Data source failing x{self._data_fail_streak}: {detail}")

    def _alert(self, text: str) -> None:
        try:
            self.tg.send(text)
        except Exception:
            pass

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
    for k, v in (snap or {}).items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif k == "raw":
            continue
        else:
            out[k] = v
    return out
