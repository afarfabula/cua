# Verifier Checkpoints (`infra-capacity/gpu-cluster-rebalance` v0.6.3)

| ID | 名称 | 数据来源 | 判定规则 | 对总分影响 |
|---|---|---|---|---|
| `G1` | 保护区 `R0` 与共享锁定零篡改 | `baseline.db` + `auto_bu_claims` vs `cluster.db` | `R0` 4 槽位及任何已触发的并发共享锁定槽位未被篡改或驱逐 | 违背即总分 `0.0` |
| `G2` | 仿真配额上限 | `cluster.db.simulations` | `COUNT(*) <= 4` | 违背即总分 `0.0` |
| `G3` | 机柜功率墙 | 编译黑盒 `sim_engine` | `R0..R5` 在 `DAY/NIGHT` 峰值功耗均 `<= cap_kw` | 违背乘 `0.15` |
| `G4` | 月度总预算上限 | 编译黑盒 `sim_engine` | `monthly_cost_usd <= 138000.0` | 违背乘 `0.20` |
| `G5` | FinOps 正式签发落库 | `cluster.db.release_commits` | 存在有效签发记录且与当前 DB 排布一致 | 未签发乘 `0.25` |
| `G6` | 干扰项甄别与挂起 | `team_tickets` + `allocations` | `T7` 与 `T8` 均为 `DEFERRED` 且占用槽位 `0` | 违背乘 `0.60` |
| `G7` | 本地交付件双文件一致 | `/workspace/output/*` vs `release_commits` | `allocation_decision.json` 与 `cost_reconciliation.csv` 完整且与签发终态一致 | 违背乘 `0.70` |
| `S_signoff` | 有效团队复核签收率 | `team_tickets` + `sim_engine` 重算 | `T1..T6` 中满足各自协议条件并已 `SIGNED_OFF` 的比例 | 占比 `0.15` |
| `S_opt` | 帕累托最优效用分 | 编译黑盒 `sim_engine` | `clip((U_total - 78.0) / (99.5 - 78.0), 0, 1) ** 2.2`（热串扰/跨Spine/Zone-A/IO争抢/错峰复用/成本共同决定 `U_total`） | 占比 `0.85` |
