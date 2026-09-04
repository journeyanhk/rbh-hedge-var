# 阶段 B 拆解清单 — 双市场轮换 + Lighter maker 腿改造

> 背景：阶段 A（时段闸门 + 收盘前强平 + 冻结告警闩锁 + metadata 保守叠加）已上线（commit `86ac9d4`）。
> 阶段 B 的目标：让引擎在 **XAUS（周内闭市的 swap，零 funding、点差半、积分 2x）** 和
> **XAU（24/7 的 perp，有 funding）** 两个市场之间自动轮换——XAUS 开市时优先吃它的低磨损，
> XAUS 闭市（周末/节假日）时自动切回 XAU perp 继续赚 funding 价差，做到 **7×24 不空转**。
> 同时把 Lighter 腿从 taker 改成 maker，这是磨损减半的大头。
>
> 每个条目都带 **验收标准** 和 **测试要求**；顺序即建议实施顺序（B1 是所有后续的地基）。

---

## B0. 前置：本周末观察 + 上线前验证（在写 B1 之前完成）

- [ ] **确认阶段 A 闸门在服务器上真的生效**：周末休市前日志出现 `no_entry:var_market_closing_in_...s`，且开着的仓位在收盘前 30min 被 `market_closing` 强平。
- [ ] **周一劳动节实战验证 metadata 叠加**：2026-09-07 XAUS 提前到 18:30 UTC 收市，观察是否收到 `📅 XAUS schedule drift` 告警并在 18:00 UTC 前后强平（这是 P1-2 的第一次真实考验）。
- [ ] **XAUS `probe-quote` 报价结构验证**（阶段 A 遗留、arm 前必做）：在服务器跑一次 probe-quote，确认报价 leg 形状正常、可成交，再考虑 arm 上量。
- [ ] **抓一份真实 XAUS metadata 原始行**（`var.asset()["raw"]`）存档，确认 `next_close_at` 的真实字段名/格式，必要时补进 `market_hours._META_CLOSE_PATHS`。

---

## B1. 地基：把「市场上下文」从进程级变成每轮独立（round-scoped）

当前 `engine.var_symbol` / `engine.lighter_symbol` 是进程启动时定死的单一值。轮换要求「这一轮用哪个市场」由开仓时决定并写进 state。

- [ ] **state 增加 `round_market` 字段**：开仓时写入本轮实际使用的 Variational 市场（`"XAUS"` 或 `"XAU"`），随 `legs`/`direction` 一起持久化。
- [ ] **`fetch_snapshot` 支持按市场取数**：把写死的 `self.var_symbol` 改为「候选市场」参数化——IDLE 阶段要能同时/择一评估两个市场（见 B2）；HOLDING 阶段只评估 `round_market`。
- [ ] **`reconcile` / `watchdog` / MTM / funding 累计全部读 `round_market`**：确保持仓期的每一处「Variational 那条腿」都用本轮市场的 symbol，不再用进程默认。
- [ ] **崩溃恢复兼容**：`_recover(ENTERING/EXITING)` 从 state 读 `round_market`；旧 state 无该字段时回退到 `engine.var_symbol`（向后兼容）。
- **验收**：开一轮 XAUS、再开一轮 XAU，state.json 各自记录正确 `round_market`；重启后恢复不串市场。
- **测试**：`test_round_market_persisted_and_recovered`、`test_hold_tick_uses_round_market_symbol`。

---

## B2. 市场选择器：IDLE 时挑一个市场开仓

- [ ] **新增 `market_selector.py`**：输入两个市场的快照（open 状态、funding 价差/carry、点差、积分权重），输出 `(chosen_market, direction, reason)` 或「都不开」。
- [ ] **优先级规则**（可配置权重）：
  1. 市场必须 **open 且不在 close_buffer 内**（复用阶段 A 的 session 判断，逐市场评估）；
  2. XAUS 开市时优先（零 funding + 点差半 + 积分 2x → 净磨损更低），只要它的 carry ≥ 阈值；
  3. XAUS 闭市或 carry 不足 → 评估 XAU perp 的 funding 价差，达标则开 XAU；
  4. 两个都不达标 → `no_entry`。
- [ ] **切换滞回（hysteresis）**：避免在临界点反复横跳——例如刚从 XAU 平仓后 N 秒内不立即切 XAUS，或要求新市场的优势超过旧市场一个 margin 才切。
- [ ] **`entry_signal` 逐市场闸门**：每个候选市场都过一遍阶段 A 的 session gate（closed / closing-soon 拒绝）。
- **验收**：XAUS 开市 → 选 XAUS；周五 XAUS 收盘前 → 拒绝 XAUS 改评估 XAU；周末 → 只可能开 XAU；两者 carry 都不足 → 不开。
- **测试**：`test_selector_prefers_xaus_when_open`、`test_selector_falls_back_to_xau_when_xaus_closed`、`test_selector_hysteresis_no_flap`、`test_selector_refuses_when_both_below_threshold`。

---

## B3. 双市场对账：IDLE / preflight 检查两边残仓

当前 `_idle_flat_check` / preflight 只看单一市场。轮换后任一市场都可能留下残腿，必须两边都查。

- [ ] **`reconcile_positions` 扩展为多市场**：对 `["XAUS", "XAU"]` 各读一次 Variational 持仓 + 对应 Lighter 持仓。
- [ ] **`_idle_flat_check` 检查两个市场都 flat** 才允许开新仓；任一市场有残仓 → 记录并（live 且允许时）走既有的残仓处置 / HALT 逻辑。
- [ ] **preflight（go-live 前）双市场空仓断言**：arm 前证明两个市场都无孤立腿。
- **验收**：XAU 上人为留一条残腿，即使当前想开 XAUS，`_idle_flat_check` 也必须先发现并拦截。
- **测试**：`test_idle_flat_check_detects_residual_in_other_market`、`test_preflight_checks_both_markets`。

---

## B4. 每日结算 / 三倍 funding 规避（XAU perp 侧）

XAU perp 有 funding 结算点；某些标的周三是三倍结算，跨结算持仓风险/成本异常。

- [ ] **配置化结算时刻表**（UTC，类似 trading_hours 的模型）：标注每日结算点 + 三倍结算日。
- [ ] **结算前 buffer 内拒绝新开 XAU 仓**（避免刚开就吃一次不利结算），或强制在结算前平掉临界仓位（按 carry 正负决定）。
- [ ] **三倍结算日特殊权重**：selector 里把三倍结算的成本/收益计入 carry 估算。
- **验收**：模拟周三三倍结算临近，selector 对 XAU 的 carry 估算体现 3x，并在 buffer 内拒绝开仓。
- **测试**：`test_xau_settlement_buffer_blocks_entry`、`test_triple_settlement_weight_in_carry`。

---

## B5. Lighter maker 腿改造（磨损减半的大头，可与 B1–B4 并行）

> review17 结论点名：「同批把 Lighter maker 腿改造排上——那个才是磨损减半的大头。」
> 当前 Lighter 腿走 taker，每轮两次 taker 磨损；改成 maker 挂单可省掉 Lighter 侧的 taker 费/滑点。

- [ ] **maker 下单路径**：Lighter 腿用限价 maker 单（post-only），在可接受价位挂单等成交。
- [ ] **成交确认 + 超时回退**：maker 单在 `fill_confirm_timeout_s` 内未成交 → 回退到 taker（或撤单重挂），**绝不允许单腿裸奔**（复用现有 `NakedLegError` / watchdog）。
- [ ] **腿顺序策略**：先挂 Lighter maker，成交后再打另一条腿？还是先确定 Variational 再挂 Lighter maker？需评估哪条腿更可能滑——写进设计注释。
- [ ] **磨损重新核算**：`economics.roundtrip_cost_usdt` 区分 maker/taker 费率，`break_even_hours` 相应下降（这会放宽 selector 的开仓门槛）。
- **验收**：shadow 模式下 maker 路径的 roundtrip 成本明显低于纯 taker；maker 超时能安全回退不留裸腿。
- **测试**：`test_lighter_maker_fill_then_hedge`、`test_lighter_maker_timeout_falls_back_no_naked_leg`、`test_roundtrip_cost_maker_lower_than_taker`。

---

## B6. 面板 / 可观测性

- [ ] 面板显示 **当前活跃市场**（XAUS / XAU）、**两个市场各自的 open 状态与 carry**、**下次切换预期**。
- [ ] 日志/告警标注每次市场切换的原因（`switch: XAUS->XAU (xaus_closing)`）。
- **验收**：任一时刻能一眼看出「现在在哪个市场、为什么、下一步会不会切」。

---

## 实施顺序与里程碑

1. **B0**（本周末～周一）：观察 + 验证，拿到真实 metadata 形状。
2. **B1**（地基）：round-scoped 市场上下文——不改行为，纯重构 + 回归测试全绿。
3. **B3**（安全网）：双市场对账——在开启轮换*之前*先保证残仓可见。
4. **B2**（核心）：市场选择器 + 滞回。
5. **B4**：结算规避。
6. **B5**：Lighter maker（可与 B2–B4 并行开发，最后一起 arm）。
7. **B6**：面板收尾。

**每一步的硬门槛**：`python3 -m pytest -q` 全绿 + `ruff check src/ tests/` 干净，再进下一步。
**arm 上量前**：B0 的 probe-quote 通过 + 双市场 preflight 空仓 + 至少跑完一个「XAUS→周末切 XAU→周日切回 XAUS」的完整 shadow 轮换周期。
