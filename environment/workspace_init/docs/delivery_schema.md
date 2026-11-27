# 交付产物格式规范（`/workspace/output/`）

请同时确保本地交付文件与系统内已正式签发（Committed）的最终集群状态完全一致：

## 1. `/workspace/output/allocation_decision.json`
JSON 对象，必须包含以下字段：
```json
{
  "release_code": "REL-2026Q4-001",
  "monthly_cost_usd": 113200.0,
  "effective_cluster_utility": 100.0,
  "simulations_used": 2,
  "deferred_teams": ["T7", "T8"],
  "signed_off_teams": ["T1", "T2", "T3", "T4", "T5", "T6"],
  "assignments": [
    {"rack": "R0", "bay": 0, "shift": "24H", "team": "PROD-ONLINE-INFERENCE", "rdma": false, "priority_iops": false}
  ]
}
```
- `assignments` 必须完整包含 `R0–R5` 上所有生效槽位记录。

## 2. `/workspace/output/cost_reconciliation.csv`
按已分配或挂起的 8 个申请团队（`T1–T8`）输出汇总行，表头严格为：
```csv
team_id,status,req_bays,assigned_bays,accepted_shift,monthly_cost_usd
```
- 被挂起（`DEFERRED`）的团队 `assigned_bays` 必须为 `0`、`monthly_cost_usd` 为 `0.00`。
- 各行 `monthly_cost_usd` 之和应等于 `allocation_decision.json` 中的 `monthly_cost_usd`。
