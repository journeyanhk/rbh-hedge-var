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


def request_json(method: str, url: str, *, headers: dict[str, str] | None = None,
                 body: dict[str, Any] | list[Any] | None = None,
                 impersonate: bool = False, timeout: float = 12.0) -> HttpResult:
    """Mutating HTTP transport for Phase 2 order gateways.

    CRITICAL: this passes through ``net_guard.check`` FIRST, so while the guard
    is armed (all of Phase 1, and Phase 2 until an operator disarms it) any
    POST/DELETE raises ``WriteBlockedError`` before a socket is opened. Nothing
    can send an order by accident — the guard is the master switch.
    """
    net_guard.check(method, url)   # raises if armed and method is mutating
    payload = json.dumps(body).encode() if body is not None else None
    req_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    started = time.time()
    if impersonate and _HAS_CURL:
        resp = _curl.request(method.upper(), url, headers=req_headers, data=payload,
                             impersonate="chrome", timeout=timeout)
        rtt = (time.time() - started) * 1000.0
        return HttpResult(resp.status_code, resp.text, rtt)
    req = urllib.request.Request(url, data=payload, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode(errors="ignore")
            status = r.status
    except Exception as exc:
        body_bytes = getattr(exc, "read", lambda: b"")()
        text = body_bytes.decode(errors="ignore") if body_bytes else str(exc)
        status = getattr(exc, "code", 0) or 0
    rtt = (time.time() - started) * 1000.0
    return HttpResult(status, text, rtt)


def post_json(url: str, body: dict[str, Any] | list[Any], *, headers: dict[str, str] | None = None,
              impersonate: bool = False, timeout: float = 12.0) -> HttpResult:
    return request_json("POST", url, headers=headers, body=body,
                        impersonate=impersonate, timeout=timeout)


def has_curl() -> bool:
    return _HAS_CURL
