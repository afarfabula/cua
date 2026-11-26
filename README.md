# clearing-core/gateway-cascade-audit

跨境清结算与实时风控网关级联故障定位、Gitea 热修、动态金丝雀回归验证、CMDB 变更与多模态 SLA 合规审计闭环（Harbor CUA 任务 v2）。

## 目录结构
- `instruction.md`: 模型题面（自然语言工作委托）
- `task.toml`: Harbor 任务配置（`schema_version = "1.3"`, `version = "0.2.0"`, `difficulty = "困难"`）
- `proposal.md`: 业务场景背景与核心验收目标
- `rubric.json` / `verifier-checkpoints.md`: 15 项独立客观评分检查点（R01–R15）
- `environment/`: Docker 镜像构建与多服务启动环境（Grafana + Prometheus + Perfetto UI + `trace_processor_shell` + Gitea + Mailpit + FastAPI 审计控制台 + Live Canary Replay）
- `solution/solve.sh`: 参考解脚本（用于 `oracle` 验证满分）
- `tests/`: `test.sh` + `grade.py` 独立 Python 评分器

## 本地自测命令
```bash
harbor lint --ignore shared-verifier .
harbor run -p . -a oracle
harbor run -p . -a nop
```
