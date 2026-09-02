"""Network write-guard.

Phase 1 is read-only by contract. This module is the last line of defense:
when armed (the default in Phase 1), any attempt to issue a non-GET HTTP
request through the project's HTTP helper raises immediately, before a socket
is opened. rbh-hedge-v2 enforces the same "reads only" invariant at the
transport layer; we copy that discipline so a coding mistake cannot silently
send a live order.
"""
from __future__ import annotations

_ARMED = True
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class WriteBlockedError(RuntimeError):
    """Raised when a mutating HTTP request is attempted while the guard is armed."""


def arm() -> None:
    global _ARMED
    _ARMED = True


def disarm(confirm: str) -> None:
    """Allow mutating requests. Requires an explicit confirmation token.

    Phase 2 live execution will call ``disarm("I_UNDERSTAND_LIVE_TRADING")``
    only after every go-live gate has passed. Nothing in Phase 1 calls it.
    """
    global _ARMED
    if confirm != "I_UNDERSTAND_LIVE_TRADING":
        raise WriteBlockedError("refusing to disarm write-guard without explicit confirmation")
    _ARMED = False


def is_armed() -> bool:
    return _ARMED


def check(method: str, url: str) -> None:
    if _ARMED and method.upper() not in SAFE_METHODS:
        raise WriteBlockedError(
            f"write-guard armed: refusing {method.upper()} {url} in read-only phase"
        )
