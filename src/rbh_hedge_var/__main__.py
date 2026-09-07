"""CLI entrypoint.

Commands:
  probe        one-shot data + economics snapshot, prints JSON, exits
  once         run a single engine tick and print the result
  run          run the engine loop + monitor until interrupted
  guard-check  print net-guard status (proves writes are blocked)
  clear-halt   clear a latched drawdown HALT and reset PnL counters
  clear-cooldown  end the current COOLDOWN now -> IDLE (skip the remaining wait)
  backfill-venue-pnl  overwrite recorded round PnL with true venue-realized numbers
  reconcile    (Phase 2) print real signed positions on both venues
  preflight    (Phase 2) go-live readiness table; never disarms/trades
  verify-funding (Phase 2) prove Lighter funding cadence -> write attestation
  funding-raw  (Phase 2) dump RAW positionFunding rows + rate/USD expectation (diagnostic)
  probe-quote  (Phase 2) discover the accepted /api/quotes/indicative instrument schema (diagnostic)
  dump-asset   (Phase 2) print RAW /api/metadata/supported_assets for a symbol (is it tradeable? diagnostic)
  cancel-test  (Phase 2) prove cancel_all clears a resting post-only order on the LIVE venue (review22 diagnostic)

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


def cmd_clear_cooldown(cfg) -> int:
    from .state_machine import COOLDOWN, StateMachine
    sm = StateMachine(cfg.get("state_file", "state.json"))
    if sm.mode != COOLDOWN:
        print(json.dumps({"cooldown": False, "mode": sm.mode,
                          "message": "not in COOLDOWN; nothing to clear"}, indent=2))
        return 0
    until = sm.state.get("cooldown_until")
    sm.force_leave_cooldown()
    print(json.dumps({
        "cooldown": False,
        "cleared": True,
        "mode": sm.mode,
        "message": "COOLDOWN cleared -> IDLE. NOTE: only effective while the "
                   "service is STOPPED — a running engine holds state in memory "
                   "and rewrites state.json each tick, clobbering this. To skip a "
                   "live cooldown, prefer lowering close_cooldown_seconds (the "
                   "engine re-clamps in-progress cooldowns each tick).",
        "was_until": until,
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
                                   var_gateway=eng._var_gateway, var_symbol=eng.var_symbol)
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


def cmd_dump_asset(cfg, argv: list[str]) -> int:
    """Diagnostic: print the RAW /api/metadata/supported_assets response for a
    symbol, verbatim. Answers the pre-BTC-migration question 'does Variational
    actually LIST a tradeable BTC instrument, and with what exact instrument_type
    / funding_interval_s / settlement_asset?' — the probe-quote 'unsupported
    instrument: P-BTC-USDC-28800' reject means the guessed identity is not a
    listed market, so we need the venue's own truth. Read-only GET (impersonated
    like the gateway), no order surface touched.

    Usage: dump-asset [SYMBOL]   (defaults to config variational.symbol)"""
    from . import http_util
    vcfg = dict(cfg.get("variational", {}))
    base_url = vcfg.get("base_url", "https://omni.variational.io").rstrip("/")
    sym = (argv[1] if len(argv) > 1 and not argv[1].startswith("-")
           else vcfg.get("symbol", "XAU")).upper()
    url = f"{base_url}/api/metadata/supported_assets?cex_asset={sym}"
    print(f"# GET {url}", flush=True)
    res = http_util.get_json(url, impersonate=True, timeout=20)
    print(f"# HTTP {res.status}", flush=True)
    try:
        print(json.dumps(res.json, indent=2, ensure_ascii=False, default=str))
    except Exception:
        print(res.text[:4000])
    return 0 if res.status == 200 else 1


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


def cmd_cancel_test(cfg) -> int:
    """review22: EMPIRICALLY prove that cancel_all clears a resting post-only
    order on the LIVE Lighter venue — the exact capability the 13:09 double-hedge
    proved was broken (a silently-failed cancel left a zombie order).

    Lifecycle, all logged:
      1) read current position + open orders (read-only baseline)
      2) place ONE tiny post-only BUY well BELOW the touch — post-only guarantees
         it rests (never crosses/fills), and 'below the touch' means an adverse
         move would have to be huge+instant to fill it before we cancel
      3) confirm via open_orders that it is actually RESTING
      4) cancel_all(symbol) — surfaces any error LOUDLY (no silent swallow)
      5) poll open_orders until PROVEN empty or the verify window expires
      6) print PASS (cancel verified) / FAIL (zombie survived) and, as a safety
         net, always fire one more cancel_all + re-arm the guard in finally

    Safe to run while HOLDING a round: the probe order is a fresh tiny resting
    order far from the market that we cancel within seconds; it does not touch
    the open hedge legs. Exits non-zero on FAIL so it is scriptable."""
    from . import economics
    from .numeric import ZERO, D

    eng = Engine(cfg)
    signer = eng._lighter_signer
    if signer is None:
        print("cancel-test: no live signer (needs config.live_trading=true + Lighter creds)")
        eng.close()
        return 1
    sym = eng.lighter_symbol
    read = eng.lighter

    def _dump_open(tag: str):
        orders = signer.open_orders(sym)
        print(f"[{tag}] open_orders({sym}) = {json.dumps(orders, default=str, ensure_ascii=False)}")
        return orders

    verify_timeout = float(cfg.get("maker_cancel_verify_timeout_s", 5))
    verify_poll = float(cfg.get("maker_cancel_verify_poll_s", 0.5))
    below_pct = D(str(cfg.get("cancel_test_below_pct", "0.03")))   # rest 3% below bid
    notional = D(str(cfg.get("cancel_test_notional_usdt", 50)))

    contract = read.public_contract(sym)
    sd = contract.get("size_decimals")
    size_step = D(1).scaleb(-int(sd)) if sd is not None else D("0.0001")
    book = read.order_book(sym)
    bids = book.get("bids") or []
    if not bids:
        print("cancel-test: no bids in the Lighter book — cannot pick a resting price")
        eng.close()
        return 1
    best_bid = D(bids[0][0])
    px = best_bid * (D(1) - below_pct)                 # well behind the touch
    qty = economics.qty_for_notional(notional, px, size_step)
    if qty <= ZERO:
        qty = size_step
    print(f"cancel-test {sym}: best_bid={best_bid} -> post-only BUY {qty} @ {px} "
          f"(~{below_pct * 100}% below), verify<= {verify_timeout}s")

    result = 1
    net_guard.disarm(LIVE_ARM_TOKEN)
    try:
        base_pos = signer.signed_position(sym)
        print(f"[baseline] signed_position({sym}) = {base_pos}")
        _dump_open("baseline")

        placed = signer.place_post_only_limit_order(sym, "buy", qty, px, reduce_only=False)
        print(f"[place] {json.dumps(placed, default=str, ensure_ascii=False)}")

        resting = []
        deadline = time.time() + verify_timeout
        while time.time() < deadline:
            resting = _dump_open("after-place")
            if resting:
                break
            time.sleep(verify_poll)
        if not resting:
            print("cancel-test: WARN — placed order never showed as resting "
                  "(either it filled, or open_orders cannot see it). Cancelling anyway.")

        try:
            cancelled = signer.cancel_all(sym)
            print(f"[cancel_all] {json.dumps(cancelled, default=str, ensure_ascii=False)}")
        except Exception as exc:
            print(f"[cancel_all] RAISED {type(exc).__name__}: {exc}")

        empty = False
        deadline = time.time() + verify_timeout
        while True:
            left = _dump_open("after-cancel")
            if not left:
                empty = True
                break
            if time.time() >= deadline:
                break
            time.sleep(verify_poll)

        pos_now = signer.signed_position(sym)
        moved = D(pos_now) - D(base_pos)
        if empty and abs(moved) <= size_step / D(2):
            print("cancel-test: PASS ✅ cancel_all verifiably cleared the resting "
                  "order and position is unchanged — maker leg is safe to re-enable.")
            result = 0
        else:
            print(f"cancel-test: FAIL ❌ empty={empty} position_moved={moved}. "
                  "cancel_all did NOT provably clear the order (or it filled). "
                  "Keep lighter_maker_enabled=false and investigate the SDK/venue.")
            result = 2
    except Exception as exc:
        print(f"cancel-test: ERROR {type(exc).__name__}: {exc}")
        result = 3
    finally:
        try:
            signer.cancel_all(sym)   # safety net: never leave a probe order resting
        except Exception:
            pass
        net_guard.arm()
        eng.close()
    return result


def cmd_backfill_venue_pnl(cfg, argv: list[str]) -> int:
    """Overwrite recorded round PnL with the true venue-realized number.

    Usage:
      python -m rbh_hedge_var backfill-venue-pnl --set 5=-1.34 [--set 6=-0.51 ...]
      python -m rbh_hedge_var backfill-venue-pnl --from realized.json  # {"5":-1.34}
    Run with the service STOPPED so the engine does not clobber the state patch.
    """
    from .state_machine import StateMachine
    mapping: dict[int, float] = {}
    if "--from" in argv:
        i = argv.index("--from")
        path = argv[i + 1] if i + 1 < len(argv) else ""
        try:
            raw = json.loads(open(path, encoding="utf-8").read())
            mapping.update({int(k): float(v) for k, v in raw.items()})
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"bad --from file: {exc}"}, indent=2))
            return 2
    for i, tok in enumerate(argv):
        if tok == "--set" and i + 1 < len(argv):
            pair = argv[i + 1]
            try:
                rid, val = pair.split("=", 1)
                mapping[int(rid)] = float(val)
            except Exception:
                print(json.dumps({"ok": False, "error": f"bad --set '{pair}' (want ID=PNL)"}, indent=2))
                return 2
    if not mapping:
        print(json.dumps({"ok": False, "error": "no rounds given; use --set ID=PNL or --from file"},
                         indent=2))
        return 2
    sm = StateMachine(cfg.get("state_file", "state.json"))
    result = sm.backfill_venue_realized(mapping)
    result["ok"] = True
    result["message"] = ("venue_realized written; panel totals now prefer it. "
                         "Original pnl preserved for audit. Restart the service to "
                         "reload state.json.")
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
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
    # var-desgin9: before trusting the maker leg live, PROVE a verified cancel
    # works on the venue; a failure auto-downgrades maker -> IOC (loud) so a
    # broken cancel can never strand a zombie order in a live hedge.
    if live and not halted:
        eng.maker_preflight()
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
    if command == "clear-cooldown":
        return cmd_clear_cooldown(cfg)
    if command == "backfill-venue-pnl":
        return cmd_backfill_venue_pnl(cfg, argv)
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
    if command == "dump-asset":
        return cmd_dump_asset(cfg, argv)
    if command == "cancel-test":
        return cmd_cancel_test(cfg)
    print(f"unknown command: {command}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
