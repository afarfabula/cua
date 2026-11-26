# clearing-core/gateway-cascade-audit

跨境清结算与实时风控网关级联故障定位、Gitea 热修、动态金丝雀回归验证、CMDB 节点隔离与多模态 SLA 合规审计闭环。

## 目录结构
- `instruction.md`: 模型题面（自然语言工作委托）
- `task.toml`: Harbor 任务配置（`schema_version = "1.3"`, `version = "0.3.0"`, `difficulty = "困难"`）
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
python3 -m unittest discover -s tests -p test_topology.py
```

手动构建时，从仓库根目录执行 `docker build -t cua-fintech-gateway ./environment`。受限网络可先运行 `bash environment/fetch-binaries.sh` 预下载发布产物。构建阶段校验 SHA-256，最终镜像只包含安装后的文件。可用 `--build-arg` 覆盖 Debian、PyPI 和 Playwright 的镜像地址，或用 `--build-arg GITHUB_PROXY=` 直连发布源。
