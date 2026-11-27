# Clearing Platform 值班工作台说明

## 内网系统入口
| 系统 | 地址 | 账号 / 密码 | 说明 |
|---|---|---|---|
| Grafana | `http://127.0.0.1:3000` | `admin` / `Clearing!2026`（匿名可浏览） | `Clearing Platform Observability` 仪表盘（含 `2026-11-24` 故障与实时灰度回放面板） |
| Perfetto UI | `http://127.0.0.1:9001` | 无需登录 | 离线 Perfetto Trace 视图，已预载故障 Trace（在网页中打开 `/traces/clearing_incident.pftrace` 查看） |
| Gitea | `http://127.0.0.1:3001` | `sre_oncall` / `Clearing!2026` | `clearing-core` 组织代码库与 PR 审批 |
| Mailpit | `http://127.0.0.1:8025` | 无需登录 | 内部邮件收件箱；发信使用 SMTP `127.0.0.1:1025` |
| CMDB & SLA 审计控制台 | `http://127.0.0.1:8080` | `sre_oncall` / `Clearing!2026` | 10 节点拓扑与切流配平、时序精细钻取图表、变更灰度回放与动态复核、SLA 批次审计 |

## 工作区资料目录
- `/workspace/docs/sla_policy_v1.md`：基础 SLA 与发布制度（请注意核对邮箱中正式生效的红笔批注勘误扫描件、视觉矩阵图例及作废标识）
- `/workspace/docs/delivery_checklist_template.md`：项目交付清单与输出文件字段规范
- `/workspace/contracts/vip_merchant_framework_2026.pdf`：VIP 商户框架协议扫描件（含维护窗免责与互斥赔付条款）
- `/workspace/history/postmortem_20261018.md`：2026-10-18 历史故障复盘与节点/切流操作红线
- `/workspace/data/gateway_requests.jsonl` & `settlement_batches_raw.csv`：昨日网关流水与结算批次明细
- 故障时段 Perfetto Trace：在 Perfetto UI（`http://127.0.0.1:9001`）网页中加载 `/traces/clearing_incident.pftrace` 查看

## CMDB 控制台使用说明
1. **时序钻取**：控制台「时序钻取」面板默认展示 4 小时 5 分钟均值聚合视图（可能平滑短时异常与起病时差），将窗口缩放到 `15 分钟以内` 可查看 5 秒级精细曲线，用于识别最早起病节点与短暂抖动后自愈的节点。
2. **节点隔离与切流配平**：控制台「节点拓扑」图实时展示 10 节点调用方向（实线为主链路、虚线为预留路径）、运行版本、告警状态与容量上限进度条。隔离确认受损节点后，须通过切流权重面板将剩余活跃 `risk-engine` 节点的流量配平至 `100%`（被隔离节点权重清零，且符合容量条与历史灰度节点上限）。
3. **变更动态复核**：变更单在灰度回放通过后，首次提交审批会触发 CAB 动态复核（返回动态审计指纹 `canary_run_fingerprint` 及最新冻结窗口要求并发送确认邮件），须依据复核意见修订变更单窗口与说明后再完成二次签发。
