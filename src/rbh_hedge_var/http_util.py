"""HTTP helper shared by both venue adapters.

Uses curl_cffi (Chrome impersonation) when available — Variational sits behind
Cloudflare and rejects a plain urllib User-Agent — and falls back to urllib for
the Lighter public REST API, which is happy with either. Every request passes
through ``net_guard.check`` so a mutating call cannot escape Phase 1.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from . import net_guard

try:  # optional, only needed for Variational behind Cloudflare
    from curl_cffi import requests as _curl  # type: ignore
    _HAS_CURL = True
except Exception:  # pragma: no cover - environment dependent
    _curl = None
    _HAS_CURL = False


class HttpError(RuntimeError):
    pass


class HttpResult:
    __slots__ = ("status", "json", "text", "rtt_ms", "received_at_ms")

    def __init__(self, status: int, text: str, rtt_ms: float) -> None:
        self.status = status
        self.text = text
        self.rtt_ms = rtt_ms
        self.received_at_ms = int(time.time() * 1000)
        try:
            self.json = json.loads(text) if text else {}
        except Exception:
            self.json = {}


def get_json(url: str, *, params: dict[str, Any] | None = None,
             impersonate: bool = False, timeout: float = 12.0) -> HttpResult:
    net_guard.check("GET", url)
    if params:
        from urllib.parse import urlencode
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode(params)}"
    started = time.time()
    if impersonate and _HAS_CURL:
        resp = _curl.get(url, headers={"Accept": "application/json"},
                         impersonate="chrome", timeout=timeout)
        rtt = (time.time() - started) * 1000.0
        return HttpResult(resp.status_code, resp.text, rtt)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (rbh-hedge-var/phase1)",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode(errors="ignore")
            status = r.status
    except Exception as exc:  # normalize transport failures
        body = getattr(exc, "read", lambda: b"")()
        text = body.decode(errors="ignore") if body else str(exc)
        status = getattr(exc, "code", 0) or 0
    rtt = (time.time() - started) * 1000.0
    return HttpResult(status, text, rtt)


def has_curl() -> bool:
    return _HAS_CURL
