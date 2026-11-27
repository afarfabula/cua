今天需要将公司可调度的 **5 台 H100 GPU 机柜（`R1–R5`，每台 4 个垂直物理槽位 `Bay 0–3`，每槽 4 卡，共 80 张物理卡）** 重新分配给下季度提交申请的 8 个研发与业务团队（`T1–T8`）。

同时，机柜 `R0` 正在承载线上核心推理服务 `PROD-ONLINE-INFERENCE`（`24H` 独占 `Bay 0–3`），**严禁对 `R0` 做任何变更或迁移**。

内网共有三个独立系统可供协同处理（统一账号 `cap_lead` / 密码 `Cluster!2026`，使用说明见 `/workspace/README.md`）：
1. **Rack & Fabric Console (`http://127.0.0.1:8081`)**：查看机柜物理拓扑与供电墙、编辑槽位分配、发起 **Staging 仿真演练**并查看热力图遥测（注意：**全天最多仅限运行 6 次 Staging 仿真**）。
2. **Team Negotiation Desk (`http://127.0.0.1:8082`)**：查看 8 个团队算力需求与历史归档，与团队代表沟通调整卡数/班次/互联与 IOPS 承诺，并逐一完成 **SLA Sign-off** 或挂起处理（**Defer**）。
3. **FinOps & Release Portal (`http://127.0.0.1:8083`)**：核查部门 Q4 预算 PO 台账、核算月度电费及网络/跨区附加费（月度预算硬上限 `$138,000`），并在一切就绪后执行正式变更签发落库。

请先完整阅读 `/workspace/docs/cluster_capacity_policy.md` 与 `/workspace/docs/delivery_schema.md` 了解物理耦合规则与准入制度，然后完成：
1. 跨站核对预算 PO 与历史审批归档，甄别并挂起不符合条件或重复申请的团队工单（确保其占用槽位为 `0`）。
2. 结合各团队负载特性与条件让步空间，通过协商达成可行配额与错峰复用方案；在 **6 次仿真配额以内**结合热力图遥测消除热串扰、跨 Spine 衰减与同柜 NVMe IO 争抢，使集群真实有效算力效用与成本效率达到最高。
3. 完成全部有效业务团队的线上 **SLA Sign-off**，并在 FinOps 平台执行**正式变更签发落库**。
4. 将与系统终态严格一致的交付文件 `/workspace/output/allocation_decision.json` 与 `/workspace/output/cost_reconciliation.csv` 落盘。
