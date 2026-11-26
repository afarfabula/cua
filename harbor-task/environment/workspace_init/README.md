# Clearing Platform 值班工作台说明

## 内网系统入口
| 系统 | 地址 | 账号 / 密码 | 说明 |
|---|---|---|---|
| Grafana | `http://127.0.0.1:3000` | `admin` / `Clearing!2026`（匿名可浏览） | `Clearing Platform Observability` 仪表盘（含 `2026-11-24` 故障与实时灰度回放面板） |
| Perfetto UI | `http://127.0.0.1:9001` | 无需登录 | 离线 Perfetto Trace 视图；容器内亦预装 `trace_processor_shell` |
| Gitea | `http://127.0.0.1:3001` | `sre_oncall` / `Clearing!2026` | `clearing-core` 组织代码库与 PR 审批 |
| Mailpit | `http://127.0.0.1:8025` | 无需登录 | 内部邮件收件箱；发信使用 SMTP `127.0.0.1:1025` |
| CMDB & SLA 审计控制台 | `http://127.0.0.1:8080` | `sre_oncall` / `Clearing!2026` | 集群空间拓扑图、变更灰度回放与签发、SLA 批次审计 |

## 工作区资料目录
- `/workspace/docs/sla_policy_v1.md`：基础 SLA 与发布制度（请注意核对邮箱中正式生效的勘误与作废标识）
- `/workspace/docs/delivery_checklist_template.md`：项目交付清单与输出文件字段规范
- `/workspace/contracts/vip_merchant_framework_2026.pdf`：VIP 商户框架协议（含特定商户清算维护窗免责条款）
- `/workspace/history/postmortem_20261018.md`：2026-10-18 历史故障复盘与节点操作红线
- `/workspace/data/gateway_requests.jsonl` & `settlement_batches_raw.csv`：昨日网关流水与结算批次明细
- `/workspace/traces/clearing_incident.pftrace`：故障时段二进制 Perfetto Trace（可直接在 `http://127.0.0.1:9001` 打开或通过 `http://127.0.0.1:9001/traces/clearing_incident.pftrace` 下载查看）

## CMDB 节点核对

拓扑实线箭头表示请求从调用方发往被调用方，虚线表示预留路径。告警来自故障时段的观测延迟，节点运行状态单独显示。对照 Grafana 的 **Per-Node P99 Latency** 曲线判断异常先后，再在拓扑中选择节点执行隔离。操作后查看 CMDB 下方的 **节点状态核对**，确认非目标节点的运行状态及状态变更次数。
