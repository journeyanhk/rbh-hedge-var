"""CLI entrypoint.

Commands:
  probe        one-shot data + economics snapshot, prints JSON, exits
  once         run a single engine tick and print the result
  run          run the engine loop + monitor until interrupted
  guard-check  print net-guard status (proves writes are blocked)
  clear-halt   clear a latched drawdown HALT and reset PnL counters
  reconcile    (Phase 2) print real signed positions on both venues
  preflight    (Phase 2) go-live readiness table; never disarms/trades
  verify-funding (Phase 2) prove Lighter funding cadence -> write attestation
  funding-raw  (Phase 2) dump RAW positionFunding rows + rate/USD expectation (diagnostic)
  probe-quote  (Phase 2) discover the accepted /api/quotes/indicative instrument schema (diagnostic)

Live execution (Phase 2) is OFF unless ALL hold:
  * config.live_trading = true
  * every `preflight` check passes
  * env RBH_HEDGE_LIVE_ARM=I_UNDERSTAND_LIVE_TRADING is set for `run`
Absent any of these the engine runs in shadow (no orders).

Usage:
  python -m rbh_hedge_var <command> [--config config.json]
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import net_guard
from .config import load_config
from .engine import Engine, _display
from .monitor import serve

LIVE_ARM_TOKEN = "I_UNDERSTAND_LIVE_TRADING"


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


def cmd_clear_halt(cfg) -> int:
    from .state_machine import StateMachine
    sm = StateMachine(cfg.get("state_file", "state.json"))
    if not sm.is_halted():
        print(json.dumps({"halted": False, "message": "no HALT latched; nothing to clear"}, indent=2))
        return 0
    prior = sm.clear_halt_and_ledger()
    print(json.dumps({
        "halted": False,
        "cleared": True,
        "message": "HALT cleared; realized_pnl and daily_pnl reset "
                   "(shadow_rounds.jsonl preserved). Restart or next tick resumes.",
        "prior": prior,
    }, indent=2, default=str))
    return 0


def cmd_reconcile(cfg) -> int:
    eng = Engine(cfg)
    if eng._var_gateway is None:
        print(json.dumps({"live_stack": False,
                          "message": "no live gateways (config.live_trading false or creds missing)"}, indent=2))
        return 1
    from .reconcile import ReconcileError, reconcile_positions
    try:
        live = reconcile_positions(eng.lighter_symbol, lighter_read=eng.lighter,
                                   var_gateway=eng._var_gateway)
    except ReconcileError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, "symbol": eng.lighter_symbol,
                      "positions": {k: str(v) for k, v in live.items()}}, indent=2))
    return 0


def cmd_preflight(cfg) -> int:
    eng = Engine(cfg)
    checks = eng.preflight()
    all_ok = all(c["ok"] for c in checks if c["check"] != "write_guard_armed")
    for c in checks:
        mark = "PASS" if c["ok"] else "FAIL"
        print(f"[{mark}] {c['check']:24s} {c['detail']}")
    print("\n" + ("READY to disarm (all trade-gates pass)." if all_ok
                  else "NOT ready: resolve FAIL rows before going live."))
    return 0 if all_ok else 1


def _maybe_arm_live(cfg, eng) -> bool:
    """Disarm the write-guard ONLY when the operator has opted in explicitly AND
    preflight passes. Returns True if live orders are now enabled."""
    if not cfg.get("live_trading"):
        return False
    if os.environ.get("RBH_HEDGE_LIVE_ARM") != LIVE_ARM_TOKEN:
        msg = "live_trading=true but RBH_HEDGE_LIVE_ARM not set -> staying SHADOW."
        print(f"[run] {msg}", flush=True)
        try:
            eng.tg.send(f"⚠️ {msg}")
        except Exception:
            pass
        return False
    checks = eng.preflight()
    gate = [c for c in checks if c["check"] != "write_guard_armed"]
    if not all(c["ok"] for c in gate):
        failed = [c["check"] for c in gate if not c["ok"]]
        # review16 incident Fix-3: this downgrade is exactly how a live round can
        # end up running under an armed guard. It must be LOUD (TG), not just a
        # console line, so the operator sees it even without tailing journald.
        msg = f"preflight FAILED {failed} -> staying SHADOW (write-guard armed)."
        print(f"[run] {msg}", flush=True)
        try:
            eng.tg.send(f"⚠️ {msg}")
        except Exception:
            pass
        return False
    net_guard.disarm(LIVE_ARM_TOKEN)
    print("[run] 🔴 WRITE-GUARD DISARMED — LIVE ORDERS ENABLED.", flush=True)
    return True


def cmd_verify_funding(cfg) -> int:
    """review4 P0-D: prove Lighter's funding cadence from real settlements and
    persist a time-boxed attestation the funding-unit gate can accept."""
    eng = Engine(cfg)
    limit = int(cfg.get("funding_history_limit", 200))
    try:
        result = eng.verify_funding(limit=limit)
    finally:
        eng.close()   # release the signer's aiohttp session (one-shot command)
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_funding_raw(cfg) -> int:
    """Diagnostic: print the RAW positionFunding rows next to the quoted rate and
    the per-hour USD expectation, to adjudicate the settlement-amount question."""
    eng = Engine(cfg)
    limit = int(cfg.get("funding_raw_limit", 10))
    try:
        result = eng.funding_raw(limit=limit)
    finally:
        eng.close()
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def cmd_probe_quote(cfg) -> int:
    """Diagnostic: discover the exact accepted /api/quotes/indicative body schema.

    The venue's serde deserializer reports ONE problem at a time, and crucially a
    WRONG enum value comes back as 'unknown variant `x`, expected one of ...' —
    which LISTS the valid variants. So we POST several candidate instrument
    bodies (base metadata shape, then a range of `kind` discriminator values) and
    print each response; one run reveals the schema.

    This sends only INDICATIVE QUOTE requests (they open no position), so it is
    read-only in effect. It briefly disarms the write-guard ONLY to send the
    probes and re-arms in a finally — no order endpoint is ever touched."""
    from . import http_util, net_guard
    from .variational_client import VariationalReadOnlyClient
    from .variational_gateway import VariationalOrderGateway
    vcfg = dict(cfg.get("variational", {}))
    vcfg["funding_interval_s"] = int(cfg.get("expected_variational_funding_interval_s",
                                             vcfg.get("funding_interval_s", 14400)))
    base_url = vcfg.get("base_url", "https://omni.variational.io")
    sym = str(vcfg.get("symbol", "XAU")).upper()
    read = VariationalReadOnlyClient(base_url=base_url, symbol=sym)
    gw = VariationalOrderGateway(base_url=base_url, symbol=sym,
                                 env_file=vcfg.get("token_env_file", ".env"),
                                 cfg=vcfg, read_client=read)
    inst = gw._instrument(sym)   # instrument dict from LIVE metadata (now incl. kind)
    qty = str(cfg.get("probe_quote_qty", "0.01"))
    # inst already carries the probe-discovered schema (kind=asset_class +
    # instrument_type); try it FIRST so a clean run confirms a price or names the
    # next missing field. The rest are fallback variations if the venue moved.
    candidates: list[tuple[str, dict]] = [
        ("corrected(kind=asset_class)", inst),
    ]
    for k in ("commodity", "CMD", "equity", "index", "etf"):
        candidates.append((f"kind={k}", {**inst, "kind": k}))
    # bare shapes (diagnose which fields are still required if the above 400)
    candidates.append(("no kind", {k: v for k, v in inst.items() if k != "kind"}))
    candidates.append(("no instrument_type",
                       {k: v for k, v in inst.items() if k != "instrument_type"}))

    path = gw.paths["indicative"]
    url = gw.base_url + path
    print(f"# probing {url} qty={qty} (metadata instrument={inst})", flush=True)
    net_guard.disarm(LIVE_ARM_TOKEN)   # quote-only; re-armed in finally
    try:
        for tag, instrument in candidates:
            body = {"instrument": instrument, "qty": qty}
            try:
                headers = gw._auth_headers("POST", path, "")
                res = http_util.request_json("POST", url, headers=headers, body=body,
                                             impersonate=gw.impersonate, timeout=20)
                print(json.dumps({"tag": tag, "http": res.status,
                                  "resp": res.text[:400], "sent": instrument},
                                 ensure_ascii=False, default=str), flush=True)
            except Exception as exc:
                print(json.dumps({"tag": tag, "error": f"{type(exc).__name__}: {exc}",
                                  "sent": instrument}, ensure_ascii=False, default=str),
                      flush=True)
    finally:
        net_guard.arm()
    return 0


def cmd_run(cfg) -> int:
    eng = Engine(cfg)
    live = _maybe_arm_live(cfg, eng)
    # review13: a LIVE deploy carrying a shadow round left in state.json will be
    # MTM-modeled by the (order-less) ShadowExecutor every tick. That is safe but
    # usually unintended — surface it loudly at startup rather than silently.
    if live and eng.sm.mode in ("HOLDING", "EXITING", "ENTERING") \
            and bool(eng.sm.state.get("shadow", True)):
        warn = (f"⚠️ LIVE deploy carrying a SHADOW round (mode={eng.sm.mode}) from "
                "state.json — it sends no orders but pollutes PnL. If unintended: "
                "stop, back up & reset state.json, restart.")
        print(f"[run] {warn}", flush=True)
        try:
            eng.tg.send(warn)
        except Exception:
            pass
    # review16 incident (Fix-1): the DANGEROUS inverse — a LIVE round persisted
    # in state.json while the write-guard stayed ARMED (a restart WHILE holding
    # fails book_flat preflight and never re-arms). The engine could read it but
    # not close it; the first exit trigger stranded the state in EXITING. Refuse
    # to run half-managed: latch HALT now with precise operator guidance.
    halted = eng.halt_if_unmanageable_live_round(live)
    if halted:
        print(f"[run] STARTUP HALT: {halted} — not trading until resolved "
              "(flatten & clear-halt from flat, or fix preflight + arm).", flush=True)
    serve(cfg.get("state_file", "state.json"),
          get_snapshot=lambda: _display(eng.last_snapshot),
          host=cfg.get("monitor_bind", "127.0.0.1"),
          port=int(cfg.get("monitor_port", 8012)),
          cfg=cfg)
    interval = int(cfg.get("poll_interval_seconds", 60))
    mode = "LIVE" if live else "shadow"
    print(f"[run] {mode} engine started; poll={interval}s. Ctrl-C to stop.", flush=True)
    last_err: str | None = None
    err_streak = 0
    try:
        while True:
            # P1-6: a tick raising must never kill the loop silently. Catch,
            # log, alert (best-effort Telegram), and keep polling.
            try:
                result = eng.tick()
                last_err, err_streak = None, 0
                print(f"[tick] mode={result.get('mode')} "
                      f"action={result.get('action') or result.get('error')}", flush=True)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                print(f"[tick] UNCAUGHT {msg}", flush=True)
                # review13: don't flood TG every tick, and don't stay silent for
                # a per-minute crash loop either. Alert on the first occurrence
                # and once more when it has clearly persisted (3 in a row).
                err_streak = err_streak + 1 if msg == last_err else 1
                last_err = msg
                if err_streak in (1, 3):
                    try:
                        eng.tg.send(f"⚠️ engine tick error x{err_streak} "
                                    f"(loop continues): {msg}")
                    except Exception:
                        pass
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
    if command == "clear-halt":
        return cmd_clear_halt(cfg)
    if command == "reconcile":
        return cmd_reconcile(cfg)
    if command == "preflight":
        return cmd_preflight(cfg)
    if command == "verify-funding":
        return cmd_verify_funding(cfg)
    if command == "funding-raw":
        return cmd_funding_raw(cfg)
    if command == "probe-quote":
        return cmd_probe_quote(cfg)
    print(f"unknown command: {command}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
