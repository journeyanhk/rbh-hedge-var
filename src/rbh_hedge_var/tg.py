"""Telegram alerts — out-of-band operator notifications (P0-4).

This is the ONLY component allowed to make a non-GET network call in Phase 1,
and deliberately so: it notifies a human, it never touches an exchange. It does
NOT route through net_guard.check() (which is the trade write-guard); a coding
mistake here can at worst spam a chat, never send an order.

Design contract:
  * No-op when the bot token or chat id is missing -> safe by default, tests
    and dev boxes stay silent instead of crashing.
  * Best-effort: a failed send is swallowed and logged to stderr, never raised
    into the trading loop.
  * Short timeout so a Telegram outage cannot stall a tick.

Config/creds are read from the .env file referenced by config, keys:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Any

from .config import read_env


class TelegramNotifier:
    def __init__(self, cfg: dict[str, Any]) -> None:
        env_file = (cfg.get("telegram") or {}).get("env_file", cfg.get(
            "lighter", {}).get("account_env_file", ".env"))
        self.token = read_env(env_file, ("TELEGRAM_BOT_TOKEN",)).strip()
        self.chat_id = read_env(env_file, ("TELEGRAM_CHAT_ID",)).strip()
        self.timeout = float((cfg.get("telegram") or {}).get("timeout_s", 5))
        self.enabled = bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        """Fire a message. Returns True on apparent success, False otherwise.

        A no-op (returns False) when unconfigured — never raises.
        """
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        # Contract-as-code: this notifier may ONLY ever reach Telegram. If a future
        # edit points it elsewhere, fail loudly rather than exfiltrate anywhere.
        assert url.startswith("https://api.telegram.org/"), "tg notifier restricted to Telegram"
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode() or "{}")
                return bool(body.get("ok"))
        except Exception as exc:  # network/parse — never propagate into the loop
            print(f"[tg] send failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False
