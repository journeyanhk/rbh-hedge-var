"""Config + .env loading (no external deps)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_config(path: str = "config.json") -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def read_env(env_file: str, keys: tuple[str, ...]) -> str:
    try:
        raw = Path(env_file).read_text(errors="ignore")
    except Exception:
        return ""
    for key in keys:
        m = re.search(r"^" + re.escape(key) + r"=(.*)$", raw, re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def env_int(env_file: str, keys: tuple[str, ...]) -> int | None:
    val = read_env(env_file, keys)
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None
