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

from . import economics, funding_attest, market_hours, net_guard, reconcile, strategy, watchdog
from .config import env_int, read_env
from .lighter_client import LighterReadOnlyClient
from .live_executor import NakedLegError
from .numeric import ZERO, D, fmt
from .shadow_executor import ShadowExecutor
from .state_machine import COOLDOWN, ENTERING, EXITING, HOLDING, IDLE, StateMachine
from .tg import TelegramNotifier
from .variational_client import VariationalReadOnlyClient

SECONDS_PER_HOUR = D(3600)


class Engine:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.live_trading = bool(cfg.get("live_trading"))
        # Phase 1 (and any non-live config) force the write-guard armed. When
        # live_trading is configured we leave the guard in its current state so
        # an operator's explicit `go-live` disarm can persist; the guard still
        # defaults to armed on a fresh process, so a restart re-blocks orders
        # until the go-live gate is re-passed.
        if not self.live_trading:
            net_guard.arm()
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
        # var-desgin6: TradFi trading-hours gate for the Variational leg (XAUS
        # closes over the weekend). Config-driven UTC schedule; empty/disabled ->
        # always tradable. Round-scoped market selection is Phase B.
        self._var_hours = vcfg.get("trading_hours") or {}
        self.executor = ShadowExecutor(cfg)
        # Live execution wiring (Phase 2). Built only when live_trading is on;
        # constructing the gateways never disarms the guard or sends anything.
        self._live_executor = None
        self._var_gateway = None
        self._lighter_signer = None
        self._throwaway_signer = None
        if self.live_trading:
            self._build_live_stack(vcfg, lcfg, acct_index)
        self.sm = StateMachine(cfg.get("state_file", "state.json"))
        self.tg = TelegramNotifier(cfg)
        self.notional = D(cfg.get("notional_per_leg_usdt", 12000))
        self.last_snapshot: dict[str, Any] = {}
        self.last_error: str | None = None
        self._data_fail_streak = 0
        self._reconcile_fail_streak = 0
        self._idle_tick_count = 0
        self._last_live_positions: dict[str, Any] | None = None

    def _build_live_stack(self, vcfg: dict[str, Any], lcfg: dict[str, Any],
                          acct_index: int | None) -> None:
        """Construct the Phase 2 order gateways + live executor. Fail-closed: if
        credentials are absent we log and stay in shadow rather than crash, so a
        misconfigured live deploy degrades safely instead of trading blind."""
        from .lighter_signer import LighterSignerClient
        from .live_executor import LiveExecutor
        from .variational_gateway import VariationalOrderGateway
        try:
            env_file = lcfg.get("account_env_file", ".env")
            self._lighter_signer = LighterSignerClient(
                base_url=lcfg.get("base_url", "https://api.rh.lighter.xyz"),
                chain_id=int(lcfg.get("chain_id", 466324)),
                account_index=acct_index,
                api_key_private_key=read_env(env_file, ("LIGHTER_API_KEY_PRIVATE_KEY",)),
                api_key_index=int(read_env(env_file, ("LIGHTER_API_KEY_INDEX",)) or 0),
                read_client=self.lighter,
            )
            # funding_interval_s is part of the instrument identity in the RFQ
            # bodies (XAU=14400, not vo's hardcoded 3600). Feed the operator's
            # validated expected interval into the gateway cfg so the quote/order
            # instrument object carries the correct listing; the funding-unit gate
            # already cross-checks this value against live metadata before unlock.
            vcfg_live = {**vcfg,
                         "funding_interval_s": int(
                             self.cfg.get("expected_variational_funding_interval_s",
                                          vcfg.get("funding_interval_s", 14400)))}
            self._var_gateway = VariationalOrderGateway(
                base_url=vcfg.get("base_url", "https://omni.variational.io"),
                symbol=vcfg.get("symbol", "XAU"),
                env_file=vcfg.get("token_env_file", ".env"),
                cfg=vcfg_live,
                read_client=self.var,   # instrument identity from LIVE metadata
            )
            self._live_executor = LiveExecutor(
                self.cfg, lighter_signer=self._lighter_signer, var_gateway=self._var_gateway)
        except Exception as exc:
            self._live_executor = None
            self._log(f"[LIVE INIT] gateway build failed -> staying shadow: {exc}")

    def _executor_for(self, shadow: bool) -> Any:
        """Pick the executor for an open round by its persisted shadow flag so a
        live round is always closed by the live executor even if live gating
        flickers mid-round."""
        if shadow:
            return self.executor
        if self._live_executor is None:
            raise RuntimeError("live round persisted but no live executor available")
        return self._live_executor

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
        # review4 P0-D: inject a valid funding attestation so verify_units can
        # reach VERIFIED even though Lighter never publishes an interval.
        self.cfg["_attested_lighter_interval_s"] = self._attested_lighter_interval()
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
            # dashboard: honest LIVE/SHADOW state = live configured AND guard down.
            "live_armed": bool(self.live_trading and not net_guard.is_armed()),
            # most recent real dual-venue positions from a live reconcile/watchdog
            # (None on shadow-only deploys or before the first live read).
            "live_positions": ({k: str(v) for k, v in self._last_live_positions.items()}
                               if self._last_live_positions else None),
        })
        # var-desgin6: trading-hours session state for the Variational leg.
        sess = self._var_session()
        snap.update({
            "var_session_enabled": sess["enabled"],
            "var_market_open": sess["open"],
            "var_seconds_to_close": sess["seconds_to_close"],
            "var_seconds_to_open": sess["seconds_to_open"],
        })
        return snap

    def _var_session(self) -> dict[str, Any]:
        """Evaluate the Variational leg's trading-hours schedule at 'now' (UTC)."""
        import datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        return market_hours.evaluate(self._var_hours, now_utc)

    # ---- tick --------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        # 1) Crash-restart recovery (P0-3): resolve a persisted ENTERING/EXITING
        #    before trading. Skip while halted (no trading actions when halted).
        #    review4 P0-C: a LIVE round must reconcile real positions before it
        #    can abort, or a naked leg stays invisible.
        if not self.sm.is_halted():
            if self.sm.mode == ENTERING:
                self._recover(ENTERING)
            elif self.sm.mode == EXITING:
                self._recover(EXITING)

        # 2) Always fetch market data FIRST so the dashboard stays live even under
        #    a HALT (review3: HALT must not freeze the snapshot to a row of "-").
        try:
            snap = self.fetch_snapshot()
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._note_data_failure(self.last_error)
            return {"ok": False, "error": self.last_error, "mode": self.sm.mode,
                    "halt": self.sm.halt_reason()}
        self.last_snapshot = snap

        # Track data-source health (P0-4 alert on sustained failure).
        if snap.get("data_errors"):
            self._note_data_failure("; ".join(f"{k}:{v}" for k, v in snap["data_errors"].items()))
        else:
            self._data_fail_streak = 0

        # 3) Latched HALT (P1-5): once tripped, refuse all TRADING until a human
        #    runs `clear-halt`. Snapshot above is fresh, so the dashboard stays
        #    live; we only skip the trading decision here.
        if self.sm.is_halted():
            return {"ok": True, "mode": self.sm.mode, "halt": self.sm.halt_reason(),
                    "action": "halted", "snapshot": _display(snap)}

        # 4) Drawdown circuit breaker -> latch HALT (P1-5).
        dd = watchdog.check_drawdown(D(self.sm.today_pnl()), D(self.cfg.get("max_daily_loss_usdt", 15)))
        if not dd.ok:
            if self.sm.set_halt(dd.reason):
                self._log(f"[HALT] {dd.reason}")
                self._alert(f"🛑 HALT (drawdown): {dd.reason}. Run `clear-halt` to resume.")
            return {"ok": True, "mode": self.sm.mode, "halt": dd.reason, "snapshot": _display(snap)}

        mode = self.sm.mode
        action = "none"

        if mode == COOLDOWN:
            # Re-clamp an over-long persisted cooldown to the CURRENT configured
            # value so editing close_cooldown_seconds (+ restart) actually takes
            # effect on an in-progress cooldown instead of waiting out the old
            # absolute timestamp.
            self.sm.clamp_cooldown(int(self.cfg.get("close_cooldown_seconds", 1200)))
            if self.sm.maybe_leave_cooldown():
                action = "cooldown_elapsed->idle"

        mode = self.sm.mode
        if mode == IDLE:
            # review4 P0-C: while live, periodically prove the book is actually
            # flat between rounds — a residual leg from a botched abort would
            # otherwise never be seen (watchdog only runs while HOLDING).
            halt_action = self._idle_flat_check()
            if halt_action:
                return {"ok": True, "mode": self.sm.mode, "halt": self.sm.halt_reason(),
                        "action": halt_action, "snapshot": _display(snap)}
            should, direction, reason = strategy.entry_signal(snap, self.cfg)
            if should:
                action = self._do_entry(direction, reason, snap)
            else:
                action = f"no_entry:{reason}"

        elif mode == HOLDING:
            action = self._hold_tick(snap)

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
        executor = self._executor_for(bool(self.sm.state.get("shadow", True)))
        price_pnl = executor.mark_to_market(
            legs, snap.get("var_price") or ZERO, snap.get("lighter_price") or ZERO, book,
        )
        total_pnl = price_pnl + funding_pnl

        # review3 P0: the price legs carry a fixed sunk roundtrip cost (~2x taker
        # slippage on both legs) from the very first tick. The per-round stop-loss
        # must measure deterioration RELATIVE to the open baseline, otherwise every
        # freshly opened round trips it on tick #1. Lazily baseline if a pre-fix or
        # recovered round is missing one.
        if self.sm.state.get("entry_mtm_usdt") is None:
            self.sm.set_entry_baseline(float(price_pnl))
        baseline = D(self.sm.state.get("entry_mtm_usdt") or 0)
        adverse = total_pnl - baseline   # ~0 at open; only real deterioration/funding moves it

        snap["unrealized_price_pnl_usdt"] = price_pnl
        snap["funding_accrued_usdt"] = funding_pnl
        snap["unrealized_total_pnl_usdt"] = total_pnl
        snap["entry_mtm_usdt"] = baseline
        snap["round_pnl_vs_entry_usdt"] = adverse

        # P0-4: single-leg watchdog. Phase 1 "reality" is the shadow legs (always
        # balanced) so live_positions is None -> OK; the wiring is live for P2.
        live_positions = self._live_positions()
        if live_positions is not None:
            self._last_live_positions = dict(live_positions)   # dashboard cache
        wd = watchdog.check_single_leg(legs, live_positions)
        snap["watchdog_action"] = wd.action
        if not wd.ok:
            self._log(f"[WATCHDOG] {wd.action}: {wd.reason}")
            self._alert(f"⚠️ Watchdog {wd.action}: {wd.reason}")
            self._do_exit("watchdog_" + wd.action, snap)
            return f"watchdog_exit:{wd.action}"

        # var-desgin6: HIGHEST-priority exit — flatten an XAUS round BEFORE the
        # market closes, because a held leg freezes (RFQ stops filling) while the
        # Lighter leg keeps moving. Fires only while still OPEN (so the close can
        # actually fill); if we somehow reach a closed market while holding, the
        # leg is already frozen — alert loudly, nothing to do but wait for reopen.
        if snap.get("var_session_enabled"):
            hours_cfg = (self.cfg.get("variational") or {}).get("trading_hours") or {}
            close_buf = float(hours_cfg.get("close_buffer_seconds", 1800) or 1800)
            to_close = snap.get("var_seconds_to_close")
            if not snap.get("var_market_open"):
                self._log("[SESSION] variational market CLOSED while holding — leg frozen until reopen")
                self._alert("🛑 Variational market CLOSED with a round open — the leg is FROZEN "
                            "until reopen; the Lighter leg is unhedged against gaps.")
            elif to_close is not None and to_close < close_buf:
                self._log(f"[SESSION] variational closes in {int(to_close)}s (<{int(close_buf)}s) -> force exit")
                self._alert(f"⏰ Flattening round: XAUS closes in {int(to_close)//60}min.")
                return self._do_exit("market_closing", snap)

        # Exit priority: basis/reversal -> take-profit -> per-round stop-loss.
        reversed_now = strategy.is_spread_reversed(snap, direction)
        streak = self.sm.bump_reversal(reversed_now)            # P1-4: pass directly
        should_exit, reason = strategy.exit_signal(snap, direction, self.cfg, streak)
        if not should_exit:
            should_exit, reason = strategy.take_profit_signal(total_pnl, self.cfg)
        if not should_exit:
            # measure loss RELATIVE to entry baseline, not absolute (review3 P0)
            should_exit, reason = strategy.round_stop_loss_signal(adverse, self.cfg)
        if not should_exit:
            # review16: VALIDATION-ONLY time exit — last in the chain so a real
            # take-profit/stop-loss/reversal always wins. Guarantees a quiet
            # validation round still exercises the full close/reconcile/ledger
            # path within max_hold_hours. Disabled (0) for the standby regime.
            should_exit, reason = strategy.max_hold_exit_signal(
                self.sm.state.get("opened_at"), self.cfg)
        if should_exit:
            return self._do_exit(reason, snap)
        return "holding"

    # ---- entry / exit ------------------------------------------------------
    def _do_entry(self, direction: str, reason: str, snap: dict[str, Any]) -> str:
        # Live gate — resolve to the live executor ONLY when every condition
        # holds: live_trading configured, funding units allow it, the unit is
        # VERIFIED (if required), a live executor exists, AND the write-guard is
        # disarmed. Any failure falls back to the shadow executor (no orders).
        live = self.live_trading and bool(snap.get("live_allowed_by_units"))
        if live and self.cfg.get("require_funding_unit_verified_for_live", True) and not snap.get("funding_verified"):
            live = False
        if live and (self._live_executor is None or net_guard.is_armed()):
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

        executor = self._executor_for(shadow=not live)
        mode_tag = "LIVE" if live else "SHADOW"
        self.sm.begin_entry(direction, reason)
        book = self._safe_book()
        try:
            result = executor.open_hedge(
                direction, self.notional,
                snap.get("var_price") or ZERO, snap.get("lighter_price") or ZERO,
                size_step, book,
                var_symbol=self.var_symbol, lit_symbol=self.lighter_symbol,
            )
        except NakedLegError as exc:
            # review4 P0-A/P0-C: a leg may be live and the auto-flatten failed.
            # Do NOT roll back to IDLE as if nothing happened — latch HALT so a
            # human reconciles the book before any further trading.
            reason = f"naked_leg:{type(exc).__name__}"
            self.sm.set_halt(reason)
            self._log(f"[{mode_tag} NAKED LEG] {exc} -> HALT")
            self._alert(f"🛑 HALT: {mode_tag} entry left a naked leg: {exc}. "
                        f"Flatten manually, then `clear-halt`.")
            return f"entry_naked_leg:{type(exc).__name__}"
        except Exception as exc:
            # A live entry can raise (single leg flattened, quote rejected). Roll
            # the state back to IDLE and let the next tick re-evaluate.
            self.sm.abort_entry(f"open_failed:{type(exc).__name__}")
            self._log(f"[{mode_tag} OPEN FAILED] {exc}")
            self._alert(f"⚠️ {mode_tag} entry failed: {exc}")
            return f"entry_error:{type(exc).__name__}"
        if result.get("both_filled"):
            self.sm.confirm_hold(result["legs"])
            self.sm.state["shadow"] = bool(result.get("shadow", not live))
            # Baseline the sunk roundtrip cost so the per-round stop-loss measures
            # deterioration RELATIVE to open, not the model's fixed entry cost.
            entry_mtm = executor.mark_to_market(
                result["legs"], snap.get("var_price") or ZERO,
                snap.get("lighter_price") or ZERO, book,
            )
            self.sm.set_entry_baseline(float(entry_mtm))
            self.sm.save()
            self._log(f"[{mode_tag} OPEN] {direction} {reason} | be_hours={fmt(snap.get('break_even_hours'),2)} "
                      f"entry_mtm={fmt(entry_mtm,4)}")
            return f"{'live' if live else 'shadow'}_open:{direction}"
        self.sm.abort_entry("leg_not_filled")
        return "entry_aborted"

    def _do_exit(self, reason: str, snap: dict[str, Any]) -> str:
        # review16 incident guard (Fix-2): NEVER transition a LIVE round into
        # EXITING when its executor cannot actually trade (write-guard armed).
        # The 2026-09-04 incident did exactly that — begin_exit() persisted
        # EXITING, then close_hedge()'s self._guard() raised WriteBlockedError
        # UNCAUGHT, stranding the state machine in EXITING with both legs open;
        # the next tick's recovery then HALTed on the residual. Instead: detect
        # the un-tradeable executor FIRST, latch HALT, and STAY in HOLDING with a
        # clear reason so the round is always fully-managed-or-halted, never half.
        shadow = bool(self.sm.state.get("shadow", True))
        if not shadow and net_guard.is_armed():
            r = f"live_exit_blocked_guard_armed:{reason}"
            if self.sm.set_halt(r):
                self._log(f"[EXIT BLOCKED] live exit '{reason}' but write-guard ARMED "
                          f"-> HALT (stays HOLDING, no state stranding)")
                self._alert(f"🛑 HALT: live exit '{reason}' blocked — write-guard armed, "
                            f"cannot close. Flatten manually or re-arm, then `clear-halt`.")
            return f"exit_blocked_guard_armed:{reason}"
        self.sm.begin_exit(reason)
        if shadow:
            return self._close_and_finish(reason, snap)
        # Live close: any failure DURING the close (guard flip, network, RFQ
        # reject) must fail LOUD, not crash the tick and silently strand EXITING.
        try:
            return self._close_and_finish(reason, snap)
        except Exception as exc:
            r = f"exit_failed:{type(exc).__name__}"
            if self.sm.set_halt(r):
                self._log(f"[EXIT FAILED] live close '{reason}' raised {exc} "
                          f"-> HALT (mode=EXITING; verify venues before clear-halt)")
                self._alert(f"🛑 HALT: live close '{reason}' failed: {exc}. "
                            f"Verify both venues, flatten any residual, then `clear-halt`.")
            return f"exit_error:{type(exc).__name__}"

    def halt_if_unmanageable_live_round(self, live: bool) -> str | None:
        """Fix-1 (review16 incident): refuse to run a LIVE round half-managed.

        If state.json carries a LIVE round (shadow=False) in an active mode but
        the process did NOT arm live orders (write-guard armed — e.g. a restart
        WHILE holding fails the ``book_flat`` preflight and never re-arms), the
        engine can READ the position but cannot CLOSE it: the first exit trigger
        would raise WriteBlockedError. That is the exact time-bomb the incident
        hit. Latch HALT NOW with precise guidance so the operator flattens or
        re-arms deliberately, rather than discovering it at exit time.

        This also HARD-ENFORCES the deploy-window discipline: you may not resume
        a live round under an armed guard — flatten & restart from flat, or
        clear-halt to acknowledge. Returns the halt reason if it latched one."""
        active = self.sm.mode in (ENTERING, HOLDING, EXITING)
        live_round = not bool(self.sm.state.get("shadow", True))
        if live or not active or not live_round or self.sm.is_halted():
            return None
        reason = f"live_round_guard_armed:{self.sm.mode}"
        if self.sm.set_halt(reason):
            self._log(f"[STARTUP HALT] live round in state.json (mode={self.sm.mode}) but "
                      f"write-guard ARMED — cannot manage/close it. Flatten & clear-halt "
                      f"from flat, or fix preflight + set RBH_HEDGE_LIVE_ARM to resume.")
            self._alert(f"🛑 HALT: a LIVE round is open (mode={self.sm.mode}) but the "
                        f"write-guard is ARMED — the engine cannot close it and would "
                        f"strand on the next exit. Flatten both legs and `clear-halt` from "
                        f"flat, OR fix preflight + set RBH_HEDGE_LIVE_ARM to resume.")
        return reason

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
        executor = self._executor_for(bool(self.sm.state.get("shadow", True)))
        result = executor.close_hedge(legs, var_price, lit_price, book)
        price_pnl = float(result.get("price_pnl") or 0)
        funding_pnl = self.sm.funding_accrued()   # P0-1: booked separately
        self.sm.finish_exit(price_pnl, funding_pnl, reason,
                            int(self.cfg.get("close_cooldown_seconds", 1200)))
        # P1-5: tag the ledger line by the round's persisted shadow flag so a
        # live close is never mislabelled as shadow.
        shadow = bool(self.sm.state.get("shadow", True))
        tag = "SHADOW" if shadow else "LIVE"
        self._log(f"[{tag} CLOSE] {reason} | price_pnl={fmt(price_pnl,4)} "
                  f"funding_pnl={fmt(funding_pnl,4)} total={fmt(price_pnl + funding_pnl,4)}")
        return f"{'shadow' if shadow else 'live'}_close:{reason}"

    def _recover(self, mode: str) -> None:
        """review4 P0-C: crash-restart recovery, routed by the PERSISTED shadow
        flag. A shadow round has no exchange footprint so it rolls back on model
        state alone. A LIVE round must never guess — it reconciles real positions
        FIRST and HALTs on any residual, because a naked leg left by a botched
        entry/exit would otherwise stay invisible until it moved against us."""
        shadow = bool(self.sm.state.get("shadow", True))
        if shadow:
            if mode == ENTERING:
                self._log("[RECOVERY] shadow ENTERING on boot -> abort to IDLE")
                self.sm.abort_entry("recovered_entering")
            else:  # EXITING
                self._log("[RECOVERY] shadow EXITING on boot -> re-run shadow exit")
                try:
                    snap = self.fetch_snapshot()
                except Exception:
                    snap = self.last_snapshot or {}
                self._close_and_finish("recovered_exit", snap)
            return

        # LIVE recovery — read the truth before touching state.
        if self._var_gateway is None:
            reason = f"recovery_no_gateway:{mode}"
            if self.sm.set_halt(reason):
                self._log(f"[RECOVERY HALT] live {mode} but no gateway to reconcile")
                self._alert(f"🛑 HALT: live {mode} recovery has no gateway to verify "
                            f"positions. Verify both venues manually, then `clear-halt`.")
            return
        try:
            live = reconcile.reconcile_positions(
                self.lighter_symbol, lighter_read=self.lighter,
                var_gateway=self._var_gateway, var_symbol=self.var_symbol)
        except Exception as exc:
            reason = f"recovery_reconcile_failed:{mode}"
            if self.sm.set_halt(reason):
                self._log(f"[RECOVERY HALT] cannot reconcile on {mode} boot: {exc}")
                self._alert(f"🛑 HALT: live {mode} recovery could not read positions: {exc}. "
                            f"Verify both venues manually, then `clear-halt`.")
            return
        tol = self._size_step() / 2
        residual = {k: str(v) for k, v in live.items() if abs(D(v)) > tol}
        if not residual:
            # No footprint on either venue -> safe to roll the round back.
            if mode == ENTERING:
                self._log("[RECOVERY] live ENTERING flat on boot -> abort to IDLE")
                self.sm.abort_entry("recovered_entering_flat")
            else:
                self._log("[RECOVERY] live EXITING flat on boot -> already closed, finalize")
                self.sm.finish_exit(0.0, self.sm.funding_accrued(), "recovered_exit_flat",
                                    int(self.cfg.get("close_cooldown_seconds", 1200)))
            return

        # review16 incident Fix-2: a residual that MATCHES our ledger legs is NOT
        # an orphan — it is the hedge we were mid-closing. If we can actually
        # trade (guard disarmed via the Fix-1 resume-arm), RESUME the close rather
        # than HALT, so a restart-while-exiting self-heals. Restricted to EXITING
        # WITH recorded legs: ENTERING has no legs yet (written at confirm_hold),
        # so positions_balanced([]) would be vacuously true — we must NOT guess
        # there, HALT stays the safe answer. Guard-armed also falls through to
        # HALT (cannot trade -> the Fix-1/Fix-3 path already surfaced it).
        legs = self.sm.state.get("legs") or []
        if (mode == EXITING and legs
                and reconcile.positions_balanced(legs, live, tolerance=tol)
                and not net_guard.is_armed()):
            self._log(f"[RECOVERY] live EXITING residual matches ledger {residual} "
                      f"-> resuming close (guard disarmed)")
            try:
                snap = self.fetch_snapshot()
            except Exception:
                snap = self.last_snapshot or {}
            try:
                self._close_and_finish("recovered_exit_resume", snap)
            except Exception as exc:
                r = f"recovery_resume_close_failed:{type(exc).__name__}"
                if self.sm.set_halt(r):
                    self._log(f"[RECOVERY HALT] EXITING resume-close raised {exc}")
                    self._alert(f"🛑 HALT: EXITING resume-close failed: {exc}. Verify both "
                                f"venues, flatten any residual, then `clear-halt`.")
            return

        # Any OTHER residual (orphan / qty mismatch / cannot trade) is a possible
        # naked leg. Refuse to trade; latch HALT and let a human reconcile.
        reason = f"recovery_residual_position:{mode}"
        if self.sm.set_halt(reason):
            self._log(f"[RECOVERY HALT] residual positions on {mode} boot: {residual}")
            self._alert(f"🛑 HALT: live {mode} recovery found residual positions {residual}. "
                        f"Flatten manually, then `clear-halt`.")

    def _idle_flat_check(self) -> str | None:
        """review4 P0-C: while live, periodically prove the book is flat between
        rounds. The single-leg watchdog only runs while HOLDING, so a residual
        leg from a botched abort would never be seen from IDLE. Every N ticks we
        reconcile; any residual -> HALT. Returns a halt action string or None.

        Shadow-only deploys have no footprint, so this is a no-op there."""
        if not (self.live_trading and self._var_gateway is not None):
            return None
        every = int(self.cfg.get("idle_reconcile_every_ticks", 20))
        if every <= 0:
            return None
        self._idle_tick_count = int(getattr(self, "_idle_tick_count", 0)) + 1
        if self._idle_tick_count % every != 0:
            return None
        try:
            live = reconcile.reconcile_positions(
                self.lighter_symbol, lighter_read=self.lighter,
                var_gateway=self._var_gateway, var_symbol=self.var_symbol)
        except Exception as exc:
            # Transient read failure: log and retry next window rather than HALT.
            self._log(f"[IDLE FLAT-CHECK] reconcile failed (retry next window): {exc}")
            return None
        self._last_live_positions = dict(live)   # dashboard: last real positions
        tol = self._size_step() / 2
        residual = {k: str(v) for k, v in live.items() if abs(D(v)) > tol}
        if residual:
            reason = f"idle_residual_position:{residual}"
            if self.sm.set_halt(reason):
                self._log(f"[IDLE HALT] book not flat while IDLE: {residual}")
                self._alert(f"🛑 HALT: residual positions found while IDLE {residual}. "
                            f"Flatten manually, then `clear-halt`.")
            return "idle_flat_check_halt"
        return None

    def _attested_lighter_interval(self) -> int | None:
        """review4 P0-D: read the persisted funding-settlement attestation and,
        if it is for Lighter and unexpired, hand its observed interval to the
        funding-unit gate. Returns None when there is no valid attestation, so
        the gate stays fail-closed exactly as before."""
        att = funding_attest.valid_attestation(self.sm.funding_attestation(), "lighter")
        return int(att["interval_s"]) if att else None

    def _size_step(self) -> Decimal:
        """Lighter base size step from size_decimals; fail-closed tiny fallback."""
        try:
            c = self.lighter.public_contract(self.lighter_symbol)
            sd = c.get("size_decimals")
            if sd is not None:
                return D(1).scaleb(-int(sd))
        except Exception:
            pass
        return D("0.0000001")

    def _funding_auth_token(self) -> str | None:
        """Signed auth token for the private /api/v1/positionFunding read.

        positionFunding requires auth for a main/sub account. Use the live
        signer if the stack is built; otherwise construct a throwaway signer
        from env creds so ``verify-funding`` also works before go-live (it only
        signs locally, never trades). Returns None if creds are absent — the
        API then rejects with a clear 'auth empty' message the caller surfaces."""
        signer = self._lighter_signer
        if signer is None:
            try:
                from .lighter_signer import LighterSignerClient
                lcfg = self.cfg.get("lighter", {})
                env_file = lcfg.get("account_env_file", ".env")
                signer = LighterSignerClient(
                    base_url=lcfg.get("base_url", "https://api.rh.lighter.xyz"),
                    chain_id=int(lcfg.get("chain_id", 466324)),
                    account_index=self.lighter.account_index,
                    api_key_private_key=read_env(env_file, ("LIGHTER_API_KEY_PRIVATE_KEY",)),
                    api_key_index=int(read_env(env_file, ("LIGHTER_API_KEY_INDEX",)) or 0),
                    read_client=self.lighter,
                )
                self._throwaway_signer = signer   # tracked so close() frees it
            except Exception as exc:
                self._log(f"[VERIFY-FUNDING] no signer for auth token: {exc}")
                return None
        try:
            return signer.auth_token()
        except Exception as exc:
            self._log(f"[VERIFY-FUNDING] auth token failed: {exc}")
            return None

    def close(self) -> None:
        """Release the Lighter signer's async HTTP session(s). For one-shot CLI
        commands (verify-funding) so the process exits without an 'Unclosed
        client session' warning. Safe to call when nothing was built."""
        for sig in (self._lighter_signer, getattr(self, "_throwaway_signer", None)):
            if sig is not None and hasattr(sig, "close"):
                try:
                    sig.close()
                except Exception:
                    pass
        self._throwaway_signer = None

    def verify_funding(self, *, limit: int = 200) -> dict[str, Any]:
        """review4 P0-D: pull REAL Lighter funding settlements, prove the cadence
        is ≈ the configured interval, and persist a time-boxed attestation the
        funding-unit gate accepts in lieu of a published interval. Read-only:
        never disarms or trades. Returns a human-readable result dict."""
        expected = int(self.cfg.get("expected_lighter_funding_interval_s", 3600))
        auth = self._funding_auth_token()
        try:
            rows = self.lighter.funding_history(self.lighter_symbol, limit=limit,
                                                auth_token=auth)
        except Exception as exc:
            return {"ok": False, "reason": f"funding history fetch failed: {exc}"}
        val = funding_attest.validate_settlements(
            rows, expected_interval_s=expected,
            cadence_tolerance_pct=float(self.cfg.get("funding_cadence_tolerance_pct", 0.2)),
            min_samples=int(self.cfg.get("funding_min_samples", 3)))
        # Amount self-consistency (review11): validate each settlement against its
        # OWN row fields — change ≈ rate × position_size × mark_price — which is
        # immune to config notional/basis mistakes. A computed ratio outside
        # tolerance means we don't understand the settlement formula: HARD gate.
        mark = None
        try:
            mark = self.lighter.public_contract(self.lighter_symbol).get("mark_price")
        except Exception:
            pass
        amt = funding_attest.amount_self_consistent(
            rows, mark_price=mark,
            tolerance_pct=float(self.cfg.get("funding_amount_tolerance_pct", 0.1)))
        note = amt["reason"]
        if not val["ok"]:
            self.sm.set_funding_attestation(None)
            return {"ok": False, "reason": val["reason"], "samples": val["samples"],
                    "amount_note": note}
        if not amt["ok"]:
            self.sm.set_funding_attestation(None)
            return {"ok": False, "reason": amt["reason"], "amount_note": note}
        att = funding_attest.build_attestation(
            "lighter", int(val["observed_interval_s"]), samples=int(val["samples"]),
            detail=val["reason"],
            validity_s=int(self.cfg.get("funding_attestation_validity_s",
                                        funding_attest.DEFAULT_VALIDITY_S)))
        self.sm.set_funding_attestation(att)
        self._log(f"[VERIFY-FUNDING] attested lighter interval={att['interval_s']}s "
                  f"samples={att['samples']} expires_at={att['expires_at']}")
        return {"ok": True, "attestation": att, "amount_note": note}

    def funding_raw(self, *, limit: int = 10) -> dict[str, Any]:
        """Diagnostic: dump the RAW positionFunding rows plus the current quoted
        funding rate + per-hour USD expectation, so the amount-magnitude question
        can be adjudicated by eye (parser field vs. venue reality)."""
        auth = self._funding_auth_token()
        try:
            body = self.lighter.funding_history_raw(self.lighter_symbol, limit=limit,
                                                    auth_token=auth)
        except Exception as exc:
            return {"ok": False, "reason": f"raw funding fetch failed: {exc}"}
        rate = None
        try:
            rate = self.lighter.funding_rate(self.lighter_symbol).get("rate")
        except Exception:
            pass
        rows = (body.get("position_fundings") or body.get("fundings")
                or body.get("funding_payments") or [])
        expected_usd = (abs(D(rate)) * D(self.notional)) if rate is not None else None
        return {
            "ok": True,
            "symbol": self.lighter_symbol,
            "quoted_rate_per_interval": str(rate) if rate is not None else None,
            "notional_usdt": str(self.notional),
            "expected_usd_per_settlement": str(expected_usd) if expected_usd is not None else None,
            "raw_rows": rows,
        }

    # ---- helpers -----------------------------------------------------------
    def _safe_book(self) -> dict[str, Any] | None:
        try:
            return self.lighter.order_book(self.lighter_symbol)
        except Exception:
            return None

    def preflight(self) -> list[dict[str, Any]]:
        """Go-live readiness checks. Read-only; never disarms or trades. Returns
        a list of {check, ok, detail} so the CLI can print a pass/fail table.

        Every check must pass before an operator disarms the write-guard."""
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        add("live_trading_configured", self.live_trading,
            "config.live_trading" + ("=true" if self.live_trading else "=false (stays shadow)"))
        add("live_stack_built", self._live_executor is not None,
            "gateways constructed" if self._live_executor is not None else "gateways missing/failed")

        # Credentials present (do not print secrets). Match the configured
        # Variational auth scheme: "token" (default) reads VARIATIONAL_TOKEN;
        # "hmac" reads VARIATIONAL_API_KEY/SECRET. Checking the wrong pair gives
        # a false "missing" when the operator configured the other scheme.
        lcfg = self.cfg.get("lighter", {})
        vcfg = self.cfg.get("variational", {})
        v_env = vcfg.get("token_env_file", ".env")
        lit_pk = read_env(lcfg.get("account_env_file", ".env"), ("LIGHTER_API_KEY_PRIVATE_KEY",))
        add("lighter_signer_key", bool(lit_pk), "LIGHTER_API_KEY_PRIVATE_KEY present" if lit_pk else "missing")

        v_scheme = str(vcfg.get("auth_scheme", "token")).lower()
        if v_scheme == "token":
            v_tok = read_env(v_env, ("VARIATIONAL_TOKEN", "VARIATIONAL_API_TOKEN"))
            add("variational_api_creds", bool(v_tok),
                "VARIATIONAL_TOKEN present (token scheme)" if v_tok else "VARIATIONAL_TOKEN missing (token scheme)")
        else:
            v_key = read_env(v_env, ("VARIATIONAL_API_KEY",))
            v_sec = read_env(v_env, ("VARIATIONAL_API_SECRET",))
            add("variational_api_creds", bool(v_key and v_sec),
                "VARIATIONAL_API_KEY/SECRET present (hmac scheme)" if (v_key and v_sec)
                else "VARIATIONAL_API_KEY/SECRET missing (hmac scheme)")

        # Lighter SDK importable.
        try:
            import lighter  # type: ignore  # noqa: F401
            add("lighter_sdk_installed", True, "lighter-python importable")
        except Exception:
            add("lighter_sdk_installed", False, "pip install lighter-python on this host")

        # Funding unit verified from a fresh snapshot.
        try:
            snap = self.fetch_snapshot()
            add("funding_unit_verified", bool(snap.get("funding_verified")),
                str(snap.get("funding_unit_reason")))
        except Exception as exc:
            add("funding_unit_verified", False, f"snapshot failed: {exc}")

        # Book flat OR a resumable persisted LIVE round (review16 incident Fix-1).
        # book_flat is a COLD-START gate; applying it verbatim to a restart WHILE
        # holding made a live round un-re-armable forever (positions != 0 -> FAIL
        # -> stay shadow -> guard armed -> cannot close -> HALT on next exit).
        # Recognise OUR OWN persisted position (on-venue book matches the ledger
        # legs within half a size step) as a legitimate RESUME, so re-arming works
        # and the engine keeps managing/closing it. A true orphan residual (no
        # matching persisted round, or mismatched qty) still FAILs.
        if self._var_gateway is not None:
            try:
                live = reconcile.reconcile_positions(
                    self.lighter_symbol, lighter_read=self.lighter,
                    var_gateway=self._var_gateway, var_symbol=self.var_symbol)
                # P1-3: "flat" is within half a size step, not an arbitrary 1e-7.
                half = self._size_step() / 2
                pos = f"positions={ {k: str(v) for k, v in live.items()} }"
                legs = self.sm.state.get("legs") or []
                persisted_live = (not bool(self.sm.state.get("shadow", True))
                                  and self.sm.mode in (HOLDING, EXITING) and bool(legs))
                if all(abs(D(v)) <= half for v in live.values()):
                    add("book_flat", True, pos)
                elif persisted_live and reconcile.positions_balanced(legs, live, tolerance=half):
                    add("book_flat", True, f"resuming persisted live round ({pos})")
                else:
                    add("book_flat", False, f"unexpected residual: {pos}")
            except Exception as exc:
                add("book_flat", False, f"reconcile failed: {exc}")
        else:
            add("book_flat", False, "no gateway to reconcile")

        # Guard currently armed = orders still blocked (expected until operator disarms).
        add("write_guard_armed", net_guard.is_armed(),
            "orders blocked (disarm to go live)" if net_guard.is_armed() else "DISARMED — orders WILL send")
        return checks

    def _live_positions(self) -> dict[str, Decimal] | None:
        """Real signed positions from both venues for the single-leg watchdog.

        Only meaningful for a LIVE round (state.shadow == False): a shadow round
        has no exchange footprint, so we return None and the watchdog treats it
        as balanced.

        P1-1: a SINGLE reconcile failure is often a transient venue blip; forcing
        a protective exit on it would flap us in and out. So we alert once, keep
        holding, and only escalate to a sentinel imbalance (which forces the
        watchdog to exit) after ``reconcile_fail_streak`` CONSECUTIVE failures —
        by then the outage is real and an unknown leg is the greater danger."""
        if bool(self.sm.state.get("shadow", True)) or self._var_gateway is None:
            self._reconcile_fail_streak = 0
            return None
        try:
            live = reconcile.reconcile_positions(
                self.lighter_symbol, lighter_read=self.lighter,
                var_gateway=self._var_gateway, var_symbol=self.var_symbol)
            self._reconcile_fail_streak = 0
            return live
        except reconcile.ReconcileError as exc:
            self._reconcile_fail_streak = int(getattr(self, "_reconcile_fail_streak", 0)) + 1
            streak = self._reconcile_fail_streak
            threshold = int(self.cfg.get("reconcile_fail_streak", 3))
            self._log(f"[RECONCILE] failure {streak}/{threshold}: {exc}")
            if streak == 1:
                self._alert(f"⚠️ Position reconcile failed (will retry): {exc}")
            if streak >= threshold:
                self._alert(f"🛑 Position reconcile failed x{streak} -> forcing protective exit")
                # Sentinel imbalance: report both flat so check_single_leg sees
                # the expected legs unmatched and forces an exit.
                return {"variational": ZERO, "lighter": ZERO}
            # Transient: keep holding (treat as balanced) until the streak trips.
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
