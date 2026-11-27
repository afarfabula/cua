# Verifier 检查点清单 (v0.5.0)

本清单与 `instruction.md`、`rubric.json`、`tests/grade.py` 严格一致。

## 评分口径

- 最终奖励：14 项独立客观检查点（R01–R14），单项权重 `0.04 ~ 0.12`（≤ 0.50），总和 `1.00`，无主观项；另设 `R_PROVENANCE` 溯源门禁（权重 `0.00`，检测到终端 UID 绕过写操作时总分封顶 `0.0`）。
- 防误改护栏：非目标仓改动归零 `R04`；受保护节点状态变更扣减 `R06`/`R07`；历史 `locked=1` 改动归零 `R11`。
- 异常处理：Gitea、Mailpit、Portal DB 或真值基线不可达时抛出 `InfraError` 并返回退出码 `2`。

## 检查点明细

| 编号 | 要求 | 类型 | 检查证据 | 判定标准 | 权重 |
|---|---|---|---|---|---|
| R01 | 基础故障归因 | 客观 | `incident_postmortem.json` | `risk-engine`、`v2.8.4`、匹配阻塞 Span | 0.04 |
| R02 | 高分辨率时序钻取归因 | 客观 | `incident_postmortem.json` | `first_onset_node=="risk-node-02"`、`14:13:00–14:14:10+08:00`、`transient_recovered_node=="risk-node-04"` | 0.08 |
| R03 | 识破并关闭干扰 PR #1 | 客观 | Gitea API `pulls/1` & comments | `state=="closed"`, `merged==false`, 评论含 `180`/`220`/`single-flight`/连接说明 | 0.06 |
| R04 | Gitea 主干合入与池守卫 | 客观 | `risk-engine` HEAD & `config.py` | HEAD 更新且 `DB_POOL_SIZE <= 180`（非目标仓零误改） | 0.06 |
| R05 | 跨模块 SWE 独立回归 | 客观 | 克隆 `main` 跑 4 项回归 + `pytest` | 跨币对非阻塞、Single-Flight（1 次调用）、503 回退+未知币对报错、跨时区幂等、单测全过 | 0.12 |
| R06 | 10 节点 CMDB 隔离与保护 | 客观 | `cmdb_nodes` & `cmdb_node_status_events` | 隔离 `risk-node-02/03`，8 个受保护节点状态及历史事件零误改 | 0.06 |
| R07 | 流量权重切流配平 | 客观 | `cmdb_nodes:traffic_weight` | 隔离节点=0，活跃和=100%，`risk-node-01<=30`、`04<=45`、`05<=55` | 0.08 |
| R08 | 灰度回放通过与指纹生成 | 客观 | `change_tickets` | `CANARY_PASSED` 且生成 `CFP-` 动态指纹 | 0.07 |
| R09 | CAB 动态复核二次签发 | 客观 | `change_tickets` | `revision_count>=1`, `APPROVED`, 指纹入描述, 避开 `17:30–20:00` | 0.10 |
| R10 | 本地清洗与指纹一致性 | 客观 | `reconciliation_summary.csv` & JSON | 4 批次无 `TEST_`、匹配全部 `duplicate_risk_tx_ids`、`canary_run_fingerprint` 一致 | 0.08 |
| R11 | SLA 审计精准核算与锁库保护 | 客观 | `sla_batches` & `locked=1` 记录 | 含红笔勘误、双图图例、Clause 4.2 与互斥取高值全匹配且历史 `locked=1` 零改动 | 0.10 |
| R12 | 跨系统数值一致性 | 客观 | JSON vs CSV vs Portal DB | 总额（±0.02）与批次字段全端一致 | 0.05 |
| R13 | 复盘 JSON 结构完整性 | 客观 | `incident_postmortem.json` | `traffic_weights`、`extended_freeze_window_utc8` 与 `batches` 完整一致 | 0.06 |
| R14 | 邮件回执发送至主管 | 客观 | Mailpit `/api/v1/messages` | 发往 `sre-lead@clearing.local` 含服务名、SHA、总额与动态指纹 | 0.04 |
