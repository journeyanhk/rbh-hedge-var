"""CLI entrypoint.

Commands (all read-only / shadow in Phase 1):
  probe        one-shot data + economics snapshot, prints JSON, exits
  once         run a single engine tick and print the result
  run          run the engine loop + monitor until interrupted
  guard-check  print net-guard status (proves writes are blocked)

Usage:
  python -m rbh_hedge_var <command> [--config config.json]
"""
from __future__ import annotations

import json
import sys
import time

from . import net_guard
from .config import load_config
from .engine import Engine, _display
from .monitor import serve


def _cfg_path(argv: list[str]) -> str:
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            return argv[i + 1]
    return "config.json"


def cmd_probe(cfg) -> int:
    eng = Engine(cfg)
    snap = eng.fetch_snapshot()
    print(json.dumps(_display(snap), indent=2, default=str, ensure_ascii=False))
    return 0


def cmd_once(cfg) -> int:
    eng = Engine(cfg)
    result = eng.tick()
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_guard_check(cfg) -> int:
    from .net_guard import WriteBlockedError
    status = {"armed": net_guard.is_armed()}
    try:
        net_guard.check("POST", "https://api.rh.lighter.xyz/api/v1/sendTx")
        status["post_blocked"] = False
    except WriteBlockedError as exc:
        status["post_blocked"] = True
        status["message"] = str(exc)
    print(json.dumps(status, indent=2))
    return 0


def cmd_run(cfg) -> int:
    eng = Engine(cfg)
    serve(cfg.get("state_file", "state.json"),
          get_snapshot=lambda: _display(eng.last_snapshot),
          host=cfg.get("monitor_bind", "127.0.0.1"),
          port=int(cfg.get("monitor_port", 8012)))
    interval = int(cfg.get("poll_interval_seconds", 60))
    print(f"[run] shadow engine started; poll={interval}s. Ctrl-C to stop.", flush=True)
    try:
        while True:
            result = eng.tick()
            print(f"[tick] mode={result.get('mode')} action={result.get('action') or result.get('error')}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[run] stopped.", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    command = argv[0]
    cfg = load_config(_cfg_path(argv))
    if command == "probe":
        return cmd_probe(cfg)
    if command == "once":
        return cmd_once(cfg)
    if command == "run":
        return cmd_run(cfg)
    if command == "guard-check":
        return cmd_guard_check(cfg)
    print(f"unknown command: {command}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
