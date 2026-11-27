# 清结算故障复盘与 SLA 审计交付清单规范

请将所有本地交付产物写入 `/workspace/output/` 目录，并确保关键数据与线上 CMDB / 变更台 / SLA 审计台终态一致：

## 1. `/workspace/output/reconciliation_summary.csv`
- 按昨日（`2026-11-24 UTC+8`）待审计的每个正式批次输出汇总行（剔除测试商户及非 `2026-11-24 UTC+8` 交易）。
- 必需列：
  `batch_id,merchant_id,tier,currency,settled_amount_cny,breached_tx_count,duplicate_risk_count,penalty_cny`

## 2. `/workspace/output/incident_postmortem.json`
需包含以下核心字段（JSON 对象）：
- `root_cause_service`：真实故障源头服务名
- `onset_time_utc8`：时序精细钻取中首次出现根因拐点的准确时间（ISO-8601，含 `+08:00`）
- `culprit_version`：引发故障的发布版本号
- `bottleneck_span`：Perfetto Trace 中实际持锁阻塞的关键调用/Span 名称
- `first_onset_node`：高分辨率时序钻取中最早出现异常拐点的风控计算节点
- `transient_recovered_node`：故障期间仅出现短暂脉冲抖动随后自行恢复正常的风控节点
- `gitea_fix_commit`：合入 Gitea `main` 分支并完成灰度回放验证的最终 Commit SHA
- `isolated_nodes`：本次在 CMDB 实际隔离的节点名数组
- `traffic_weights`：切流配平后各 `risk-engine` 节点（含已隔离节点权重 `0`）的流量权重映射（整数百分比）
- `canary_run_fingerprint`：变更台灰度压测回放通过后生成的动态审计指纹
- `extended_freeze_window_utc8`：结合 CAB 动态复核意见确认后的最终生效日终冻结窗口（如 `17:30-20:00`）
- `scheduled_release_window_utc8`：变更台签发的最终发布窗口（格式如 `2026-11-24 20:15-20:45`）
- `total_penalty_cny`：昨日 4 个待审批次的合计人民币赔付金额（保留 2 位小数）
- `batches`：以 `batch_id` 为键的字典，记录各批次与控制台一致的核算结果
- `duplicate_risk_tx_ids`：去重后识别出的全部存在重复扣款风险的 `tx_id` 列表
