# 算力中心季度 H100 GPU 集群重排工作台说明

## 内网系统入口
| 系统 | 地址 | 账号 / 密码 | 说明 |
|---|---|---|---|
| Rack &amp; Fabric Console | `http://127.0.0.1:8081` | `cap_lead` / `Cluster!2026` | 机柜槽位排布、Staging 仿真（每日上限 6 次）与热力图遥测 |
| Team Negotiation Desk | `http://127.0.0.1:8082` | `cap_lead` / `Cluster!2026` | 8 个申请团队工单、历史审批归档、条件协商与 SLA 签收 |
| FinOps &amp; Release Portal | `http://127.0.0.1:8083` | `cap_lead` / `Cluster!2026` | 部门预算 PO 台账、费用核算与正式变更签发 |

## 工作区文件
- `/workspace/docs/cluster_capacity_policy.md`：机房供电、物理散热、互联拓扑与算力准入制度。
- `/workspace/docs/delivery_schema.md`：交付至 `/workspace/output/` 的最终文件格式要求。
