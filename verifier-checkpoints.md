# Verifier 检查点清单

本清单与 `instruction.md`、`rubric.json`、`tests/grade.py` 严格一致。

## 评分口径

- 最终奖励：15 项独立客观检查点（R01–R15），单项权重 `0.05 ~ 0.09`（≤ 0.50），总和 `1.00`，无主观项。
- 独立判分：单项失败不连坐，无 reward 截断；关键反作弊与反空跑护栏已内嵌于 R07/R11/R14。
- 异常处理：Gitea、Mailpit、Portal DB 或真值基线不可达时抛出 `InfraError` 并返回退出码 `2`。

## 检查点明细

| 编号 | 要求 | 类型 | 检查证据 | 判定标准 | 权重 |
|---|---|---|---|---|---|
| R01 | 识别根因服务 | 客观 | `incident_postmortem.json:root_cause_service` | `== "risk-engine"` | 0.06 |
| R02 | 识别故障起始时间与版本 | 客观 | `onset_time_utc8`, `culprit_version` | UTC+8 在 `14:12–14:16` 且含 `v2.8.4` | 0.06 |
| R03 | 识别 Perfetto 瓶颈 Span | 客观 | `bottleneck_span` | 匹配 `acquire_rule_lock` / `fx_quote_http_retry` / `_rule_cache_lock` | 0.06 |
| R04 | 关闭 Gitea 干扰 PR #1 | 客观 | Gitea API `pulls/1` & comments | `state=="closed"`, `merged==false`, 评论含 `180`/连接数 | 0.07 |
| R05 | Gitea 修复合入 `main` | 客观 | `risk-engine` `main` HEAD & `config.py` | HEAD 更新且 `DB_POOL_SIZE <= 180` | 0.08 |
| R06 | 独立回归测试全过 | 客观 | 克隆 `main` 跑并发/幂等/503/原单测 | 4 项全通过 | 0.08 |
| R07 | CMDB 拓扑隔离与保护 | 客观 | `cmdb_nodes`、`cmdb_node_status_events`、受保护的初始 Portal DB 快照 | 一半按目标节点实际隔离逐个给分。至少隔离一个目标后，另一半要求四个非目标节点与初始快照一致且无状态变更事件，误改后恢复也扣除保护分。 | 0.07 |
| R08 | 变更排期避开冻结窗 | 客观 | `change_tickets` 表 | 匹配 SHA/Span 且不重叠 `17:30–19:30` | 0.07 |
| R15 | 灰度回放通过并签发 | 客观 | `change_tickets:canary_status, status` | `CANARY_PASSED` 且 `APPROVED` | 0.08 |
| R09 | 本地对账与重复风险识别 | 客观 | `reconciliation_summary.csv` & JSON | 4 批次无 `TEST_` 且匹配全部重复风险 `tx_id` | 0.07 |
| R10 | 2026-11-24 批次精准审计 | 客观 | `sla_batches` 表 | 含 REV-2 勘误 + 合同 Clause 4.2 免责全匹配 | 0.09 |
| R11 | 历史锁库记录保护 | 客观 | `sla_batches` & `change_tickets` `locked=1` | 零改动（且已执行目标动作） | 0.06 |
| R12 | 跨系统数值一致性 | 客观 | 本地 JSON vs Portal DB | 赔付总额一致 | 0.05 |
| R13 | 邮件回执发送至主管 | 客观 | Mailpit `/api/v1/messages` | 发往 `sre-lead@clearing.local` 含 SHA 与总额 | 0.05 |
| R14 | 非目标仓库零误改 | 客观 | `settlement-worker` & `api-gateway` | `main` HEAD 未变 | 0.05 |
