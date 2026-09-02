# rbh-hedge-var

**XAU funding-rate hedge between Variational and RHC Lighter.**
Base: [`variational-ondo`](https://github.com/journeyanhk/variational-ondo) strategy engine.
Lighter接入 knowledge from [`grid-bot-wg@dev004-dy`](https://github.com/journeyanhk/grid-bot-wg/tree/dev004-dy).
Safety/state-machine/economics contracts借鉴 from [`rbh-hedge-v2`](https://github.com/Yuu69/rbh-hedge-v2).

> ## ⚠️ Phase 1 = SHADOW ONLY. This code CANNOT place a real order.
> The network write-guard is armed at all times; every order surface raises.
> Live execution is Phase 2 and does not exist in this tree yet.

---

## What it does (Phase 1)

Each tick it reads public market data from both venues, computes the hedge
economics with Decimal precision, drives a persisted state machine, and
simulates the hedge in **shadow** mode (no orders). It answers, honestly and
continuously: *"Right now, which leg should I short, would it break even, and
is it safe to go live?"*

Delivered in two phases, as agreed:

| | Phase 1 (this tree) | Phase 2 (later, gated) |
|---|---|---|
| Data | ✅ read-only Lighter + Variational | same |
| Economics | ✅ Decimal, VWAP exit, break-even | same |
| Funding-unit gate | ✅ hard gate | same |
| State machine | ✅ IDLE→ENTERING→HOLDING→EXITING→COOLDOWN | same |
| Execution | ✅ **shadow only** | real dual-leg (taker→maker) |
| Orders | ❌ blocked at transport layer | behind `net_guard.disarm(...)` |

## Design decisions baked in (per the review)

1. **Auto direction.** The side is chosen every tick from the sign of the
   hourly funding spread — short the higher-funding leg, long the lower. No
   hard-coded whitelist. (`strategy.choose_direction`)
2. **Funding-unit hard gate.** Both legs must publish a positive
   `funding_interval_s` to be VERIFIED; otherwise live is refused. Variational's
   rate is *annualized* over a *4h* interval; Lighter is a small per-interval
   decimal and **does not publish its interval** — so it stays UNVERIFIED and
   live stays blocked until Phase 2 private settlement confirms it.
   (`funding_guard`)
3. **`rfq_id` ≠ fill.** The shadow executor models accept→confirm as two steps;
   `filled` is only true after confirmation. (`shadow_executor`)
4. **VWAP exit pricing.** Exit cost walks the real order book, not the mark.
   (`economics.executable_vwap`)
5. **Single-leg watchdog + drawdown breaker.** (`watchdog`)
6. **Dedicated subaccount.** Config/`.env` assume a separate RHC subaccount from
   the grid bot — no shared nonce or margin.

## Live-data reality checks (verified while building, 2026-09-02)

- RHC Lighter lists gold as **`XAU`** (market_id 40), **not** `XAUT`.
- RHC `funding-rates` returns a per-exchange decimal **without** an interval
  field → funding-unit gate correctly fails closed.
- Variational `supported_assets` publishes an **annualized** funding figure on a
  **14400s (4h)** interval → normalized with VO's `|rate|>0.01 ⇒ annualized`
  heuristic. Getting this wrong is the >5× error the design review warned about.

## Quick start

```bash
python3 -m pip install -r requirements.txt   # curl_cffi (Variational) + pytest
cp .env.example .env                          # Phase 1 needs no secrets filled

python3 run.py guard-check   # prove writes are blocked
python3 run.py probe         # one-shot data + economics snapshot (JSON)
python3 run.py once          # run a single engine tick, persist state.json
python3 run.py run           # loop + read-only monitor on 127.0.0.1:8012
python3 -m pytest -q         # 35 tests
```

The monitor is loopback-only and read-only, mirroring VO's 8011 / rbh's 8010.

## Layout

```
src/rbh_hedge_var/
  numeric.py          Decimal helpers (money/size never touch float)
  net_guard.py        transport-layer write-guard (armed in Phase 1)
  http_util.py        GET-only HTTP (curl_cffi impersonation for Variational)
  config.py           config.json + .env loader
  lighter_client.py   RHC Lighter READ-ONLY adapter (api.rh.lighter.xyz, 466324)
  variational_client.py  Variational READ-ONLY metadata adapter
  funding_guard.py    funding-interval unit hard gate + hourly normalization
  strategy.py         snapshot, AUTO direction, entry/exit signals
  economics.py        VWAP, roundtrip cost, break-even projection
  state_machine.py    persisted IDLE→ENTERING→HOLDING→EXITING→COOLDOWN
  shadow_executor.py  models the hedge, NEVER sends an order
  watchdog.py         single-leg exposure + drawdown circuit breaker
  engine.py           per-tick wiring
  monitor.py          loopback read-only dashboard
  __main__.py         CLI
tests/                35 unit tests
docs/                 ARCHITECTURE, PHASE2_PLAN
GO_LIVE_CHECKLIST.md  the gate that must pass before Phase 2 goes live
```

## Going live

Do **not** flip `live_trading` and expect orders — Phase 1 has no executor.
Phase 2 work and the mandatory pre-live gates are in
[`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md) and
[`docs/PHASE2_PLAN.md`](docs/PHASE2_PLAN.md). Nothing sends an order until every
item there is checked and `net_guard.disarm("I_UNDERSTAND_LIVE_TRADING")` is
called from the (not-yet-written) live executor.
