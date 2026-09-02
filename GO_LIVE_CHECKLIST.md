# GO-LIVE CHECKLIST

Adapted from `rbh-hedge-v2`'s go-live discipline. **Every box must be checked
before Phase 2 live execution is enabled.** Until then the code is shadow-only
and the network write-guard stays armed.

## 0. Isolation (hard prerequisites)
- [ ] RHC Lighter **dedicated subaccount** for the hedge bot (separate
      `LIGHTER_ACCOUNT_INDEX` from the grid bot).
- [ ] Dedicated `LIGHTER_API_KEY_INDEX` in 4..254, **not shared** with the grid
      bot (avoids nonce collisions).
- [ ] Separate `.env`, separate process, separate margin pool.

## 1. Funding-unit verification (the >5× economic risk)
- [ ] Variational funding口径 confirmed from the API: annualized figure over
      **14400s (4h)**. `funding_guard` expects `expected_variational_funding_interval_s=14400`.
- [ ] RHC Lighter funding interval **proven from private positionFunding
      settlement** (public `funding-rates` omits it). Only then set
      `expected_lighter_funding_interval_s` to the observed value and let the
      gate report VERIFIED.
- [ ] `require_funding_unit_verified_for_live=true` (never relax this).

## 2. Round-trip cost measured (not assumed)
- [ ] Measure real round-trip cost with a **minimal-notional** live probe:
      Variational RFQ embedded spread ×2 + Lighter slippage + fees.
- [ ] Replace `assumed_roundtrip_cost_pct` with the measured value; re-check the
      break-even projection.

## 3. Execution contracts (Phase 2 code, see docs/PHASE2_PLAN.md)
- [ ] Lighter signer wired via `lighter-python` `SignerClient` (RHC params:
      url `https://api.rh.lighter.xyz`, chainId 466324, api_key_index 4..254).
- [ ] Serial nonce management for the hedge subaccount.
- [ ] Market entry = **IOC aggressive limit with slippage cap** (Phase 1 taker),
      post-order fill confirmation by polling client-order-id.
- [ ] `rfq_id` treated as *accepted, not filled*; fill proven by private
      position/fill reconciliation on BOTH legs.
- [ ] Exit priced on executable VWAP; illiquid (Variational) leg closed first.

## 4. Risk gates
- [ ] Single-leg exposure watchdog live-wired to real positions from both
      venues (`watchdog.check_single_leg` with live_positions).
- [ ] Drawdown circuit breaker (`max_daily_loss_usdt`) halts and flattens.
- [ ] Variational token-expiry alerting.
- [ ] Funding-settlement-time awareness: avoid opening right after settlement.

## 5. Staged rollout
- [ ] `paper` shadow observed for ≥1 week; direction/break-even sane vs market.
- [ ] Minimal-notional live for round-trip cost, then re-derive break-even.
- [ ] Scale notional only after measured cost confirms a positive edge.
- [ ] `rbh-hedge-v2` running alongside in shadow as an independent monitor; when
      its gates say "stale/thin" and this bot says "enter", trust the gates.

## 6. Final arming
- [ ] All boxes above checked.
- [ ] `docs/PHASE2_STATUS.md` REVIEW-REQUIRED items (Variational REST paths +
      signing, Lighter SDK method surface, nonce, fill confirmation) verified.
- [ ] `live_trading=true` **and** `funding_verified=true` at runtime.
- [ ] `python3 run.py preflight` is all-green (except the expected armed-guard row).
- [ ] `run` disarms only via the explicit opt-in
      `RBH_HEDGE_LIVE_ARM=I_UNDERSTAND_LIVE_TRADING` after `preflight` passes;
      no other code path calls `net_guard.disarm`.
