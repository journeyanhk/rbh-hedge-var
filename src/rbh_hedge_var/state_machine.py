"""Explicit, persisted state machine.

    IDLE -> ENTERING -> HOLDING -> EXITING -> COOLDOWN -> IDLE

The design review named this the #1 prerequisite for full automation: VO's
implicit state produced ambiguous states after a crash-restart. Here the state
is written atomically to state.json on every transition, so a restart resumes
deterministically (recovery = "new trading disabled until reconciled", a
Phase 2 hook that reads this file).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

IDLE = "IDLE"
ENTERING = "ENTERING"
HOLDING = "HOLDING"
EXITING = "EXITING"
COOLDOWN = "COOLDOWN"

VALID_TRANSITIONS = {
    IDLE: {ENTERING},
    ENTERING: {HOLDING, EXITING, IDLE},   # IDLE = entry aborted/rolled back
    HOLDING: {EXITING},
    EXITING: {COOLDOWN, HOLDING},         # HOLDING = exit aborted (single leg left)
    COOLDOWN: {IDLE},
}


def _utc_day(ts: float | None = None) -> str:
    """UTC date key (P1-7) so daily PnL aligns with funding settlement (UTC)."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


def empty_state() -> dict[str, Any]:
    return {
        "mode": IDLE,
        "round_id": 0,
        "direction": None,
        "legs": [],
        "opened_at": None,
        "cooldown_until": None,
        "reversal_streak": 0,
        "realized_pnl": 0.0,
        "funding_accrued_usdt": 0.0,   # estimated funding for the open round
        "funding_last_accrual_ts": None,  # last time funding was accrued (P0-1)
        "entry_mtm_usdt": None,        # price-leg MTM at open (sunk roundtrip cost baseline)
        "daily_pnl": {},
        "round_history": [],
        "shadow": True,
        "halt": None,                  # {reason, at} once tripped; needs manual clear
        "last_reason": None,
        "last_update": None,
    }


class StateMachine:
    def __init__(self, path: str) -> None:
        self.path = path
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        p = Path(self.path)
        if not p.exists():
            return empty_state()
        try:
            base = empty_state()
            base.update(json.loads(p.read_text()))
            return base
        except Exception:
            return empty_state()

    def save(self) -> None:
        self.state["last_update"] = int(time.time())
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False, default=str))
        tmp.replace(p)

    @property
    def mode(self) -> str:
        return self.state.get("mode", IDLE)

    def can_transition(self, target: str) -> bool:
        return target in VALID_TRANSITIONS.get(self.mode, set())

    def transition(self, target: str, reason: str = "") -> None:
        if not self.can_transition(target):
            raise ValueError(f"illegal transition {self.mode} -> {target} ({reason})")
        self.state["mode"] = target
        self.state["last_reason"] = reason
        self.save()

    # ---- lifecycle helpers -------------------------------------------------
    def begin_entry(self, direction: str, reason: str) -> None:
        self.transition(ENTERING, reason)
        self.state["direction"] = direction
        self.state["reversal_streak"] = 0
        self.save()

    def confirm_hold(self, legs: list[dict[str, Any]]) -> None:
        self.transition(HOLDING, "both legs filled (shadow)")
        self.state["round_id"] = int(self.state.get("round_id", 0)) + 1
        self.state["legs"] = legs
        self.state["opened_at"] = int(time.time())
        self.state["funding_accrued_usdt"] = 0.0
        self.state["funding_last_accrual_ts"] = int(time.time())
        self.state["entry_mtm_usdt"] = None   # engine fills the baseline right after
        self.save()

    def set_entry_baseline(self, price_mtm_usdt: float) -> None:
        """Record the price-leg MTM at open. This is the sunk roundtrip cost the
        shadow model books immediately (~2x taker slippage on both legs); the
        per-round stop-loss measures deterioration RELATIVE to this baseline so a
        freshly opened round is not instantly stopped out (review3 P0)."""
        self.state["entry_mtm_usdt"] = float(price_mtm_usdt)
        self.save()

    def accrue_funding(self, amount_usdt: float) -> float:
        """Add an estimated funding increment to the open round (P0-1).

        Returns the new cumulative funding for the round. Only meaningful while
        HOLDING; callers gate on mode.
        """
        cur = float(self.state.get("funding_accrued_usdt", 0.0)) + float(amount_usdt)
        self.state["funding_accrued_usdt"] = cur
        self.save()
        return cur

    def funding_accrued(self) -> float:
        return float(self.state.get("funding_accrued_usdt", 0.0))

    def abort_entry(self, reason: str) -> None:
        self.transition(IDLE, f"entry_aborted:{reason}")
        self.state["direction"] = None
        self.state["legs"] = []
        self.save()

    def begin_exit(self, reason: str) -> None:
        self.transition(EXITING, reason)

    def finish_exit(self, price_pnl: float, funding_pnl: float, reason: str,
                    cooldown_s: int) -> None:
        """Close a round. Round PnL = price leg PnL + accrued funding (P0-1).

        Both components are stored separately in history and in the append-only
        shadow_rounds.jsonl so the shadow ledger reflects the funding income
        that is the entire point of the strategy.
        """
        self.transition(COOLDOWN, reason)
        total = float(price_pnl) + float(funding_pnl)
        day = _utc_day()
        self.state.setdefault("daily_pnl", {})
        self.state["daily_pnl"][day] = float(self.state["daily_pnl"].get(day, 0.0)) + total
        self.state["realized_pnl"] = float(self.state.get("realized_pnl", 0.0)) + total
        record = {
            "round_id": self.state.get("round_id"),
            "direction": self.state.get("direction"),
            "opened_at": self.state.get("opened_at"),
            "closed_at": int(time.time()),
            "reason": reason,
            "price_pnl": float(price_pnl),
            "funding_pnl": float(funding_pnl),
            "pnl": total,
            "shadow": bool(self.state.get("shadow", True)),
        }
        history = list(self.state.get("round_history") or [])
        history.append(record)
        self.state["round_history"] = history[-20:]     # bounded live view
        self._append_shadow_round(record)               # unbounded ledger (P1-6)
        self.state["direction"] = None
        self.state["legs"] = []
        self.state["opened_at"] = None
        self.state["reversal_streak"] = 0
        self.state["funding_accrued_usdt"] = 0.0
        self.state["entry_mtm_usdt"] = None
        self.state["cooldown_until"] = int(time.time()) + int(cooldown_s)
        self.save()

    def _append_shadow_round(self, record: dict[str, Any]) -> None:
        """Append-only JSONL ledger next to state.json (P1-6)."""
        try:
            p = Path(self.path).parent / "shadow_rounds.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # ledger is best-effort; never block the state machine

    def maybe_leave_cooldown(self) -> bool:
        if self.mode != COOLDOWN:
            return False
        until = self.state.get("cooldown_until") or 0
        if int(time.time()) >= int(until):
            self.transition(IDLE, "cooldown_elapsed")
            self.state["cooldown_until"] = None
            self.save()
            return True
        return False

    def bump_reversal(self, reversed_now: bool) -> int:
        streak = int(self.state.get("reversal_streak", 0))
        streak = streak + 1 if reversed_now else 0
        self.state["reversal_streak"] = streak
        self.save()
        return streak

    def today_pnl(self) -> float:
        return float(self.state.get("daily_pnl", {}).get(_utc_day(), 0.0))

    # ---- halt latch (P1-5) -------------------------------------------------
    def is_halted(self) -> bool:
        return bool(self.state.get("halt"))

    def halt_reason(self) -> str | None:
        h = self.state.get("halt")
        return h.get("reason") if isinstance(h, dict) else None

    def set_halt(self, reason: str) -> bool:
        """Latch a HALT. Returns True if this is a NEW halt (for one-shot alert)."""
        if self.state.get("halt"):
            return False
        self.state["halt"] = {"reason": reason, "at": int(time.time())}
        self.save()
        return True

    def clear_halt(self) -> None:
        self.state["halt"] = None
        self.save()

    def clear_halt_and_ledger(self) -> dict[str, Any]:
        """Clear the HALT latch AND zero the in-memory PnL counters so the next
        tick does not immediately re-halt on the same distorted daily loss. The
        append-only shadow_rounds.jsonl is left intact as the historical record.
        Returns what was cleared for operator visibility."""
        prior = {
            "halt": self.state.get("halt"),
            "realized_pnl": self.state.get("realized_pnl"),
            "daily_pnl": dict(self.state.get("daily_pnl") or {}),
        }
        self.state["halt"] = None
        self.state["realized_pnl"] = 0.0
        self.state["daily_pnl"] = {}
        self.save()
        return prior
