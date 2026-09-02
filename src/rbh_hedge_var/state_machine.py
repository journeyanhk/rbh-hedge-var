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
        "daily_pnl": {},
        "round_history": [],
        "shadow": True,
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
        self.save()

    def abort_entry(self, reason: str) -> None:
        self.transition(IDLE, f"entry_aborted:{reason}")
        self.state["direction"] = None
        self.state["legs"] = []
        self.save()

    def begin_exit(self, reason: str) -> None:
        self.transition(EXITING, reason)

    def finish_exit(self, pnl: float, reason: str, cooldown_s: int) -> None:
        self.transition(COOLDOWN, reason)
        day = time.strftime("%Y-%m-%d")
        self.state.setdefault("daily_pnl", {})
        self.state["daily_pnl"][day] = float(self.state["daily_pnl"].get(day, 0.0)) + float(pnl)
        self.state["realized_pnl"] = float(self.state.get("realized_pnl", 0.0)) + float(pnl)
        history = list(self.state.get("round_history") or [])
        history.append({
            "round_id": self.state.get("round_id"),
            "direction": self.state.get("direction"),
            "opened_at": self.state.get("opened_at"),
            "closed_at": int(time.time()),
            "reason": reason,
            "pnl": float(pnl),
            "shadow": bool(self.state.get("shadow", True)),
        })
        self.state["round_history"] = history[-20:]
        self.state["direction"] = None
        self.state["legs"] = []
        self.state["opened_at"] = None
        self.state["reversal_streak"] = 0
        self.state["cooldown_until"] = int(time.time()) + int(cooldown_s)
        self.save()

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
        return float(self.state.get("daily_pnl", {}).get(time.strftime("%Y-%m-%d"), 0.0))
