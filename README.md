# infra-capacity/gpu-cluster-rebalance (v0.6.0)

精简高区分度 CUA 任务：**编译隐藏非线性物理内核 + 6 次仿真预算门禁 + 三站点跨系统协商与可视化热力图诊断 + 帕累托凸函数评分**。

## 目录结构
- `environment/engine/sim_engine.c`：C99 编译态黑盒集群物理与成本引擎（构建阶段编译为 stripped 二进制 `chmod 700`，运行态镜像无源码）。
- `environment/services/`：3 个独立轻量 FastAPI 业务台（`:8081` Rack Console、`:8082` Team Negotiation Desk、`:8083` FinOps & Release Portal）共享受保护 SQLite 数据库。
- `tests/grade.py` & `tests/test_cluster_calibration.py`：客观门禁与帕累托凸函数评测及多档策略分布校准测试。
- `solution/solve.py`：端到端 Oracle 求解验证脚本。
