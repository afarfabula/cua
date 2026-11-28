# 算力中心季度 H100 GPU 集群重排工作台说明

## 内网系统入口（统一账号 `cap_lead` / 密码 `Cluster!2026`）
| 系统 | 地址 | 职责与操作说明 |
|---|---|---|
| Rack & Fabric Console | `http://127.0.0.1:8081` | 查看 6 台机柜（`R0–R5`，每台 `Bay 0–3` 共 4 槽、每槽 4 卡）拓扑与供电墙、编辑槽位分配、运行 **Staging 仿真演练（全天上限 4 次）** 并查看热力图遥测 |
| Team Negotiation Desk | `http://127.0.0.1:8082` | 查看 8 个团队（`T1–T8`）算力申请与历史审批归档、协商卡数/班次/RDMA/优先 IOPS 条件、挂起无效或重复单（**Defer**，槽位清零）并完成有效团队 **SLA Sign-off** |
| FinOps & Release Portal | `http://127.0.0.1:8083` | 核查部门 Q4 预算 PO 台账、核算月度班次/RDMA/跨区费用（月度预算硬上限 `$138,000`），在全部有效团队签收后执行**正式变更签发落库** |

## 工作区规范与交付要求
- `/workspace/docs/cluster_capacity_policy.md`：机房供电墙、`R0` 保护区红线、预算准入制度、仿真配额与物理耦合诊断说明。
- `/workspace/docs/delivery_schema.md`：最终交付至 `/workspace/output/` 的文件格式规范（须同时落盘 `allocation_decision.json` 与 `cost_reconciliation.csv`，且与系统已正式签发的终态严格一致）。
