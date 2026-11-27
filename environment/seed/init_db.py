import json
import os
import pathlib
import secrets
import shutil
import sys

SERVICES_DIR = pathlib.Path(__file__).resolve().parents[1] / "services"
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from common import (  # noqa: E402
    DB_PATH,
    RACK_SPECS,
    SECRET_FILE,
    get_conn,
    init_schema,
)


def seed_database(
    db_path: pathlib.Path = DB_PATH,
    baseline_path: pathlib.Path = pathlib.Path("/var/lib/cluster-ops/baseline.db"),
    truth_path: pathlib.Path = pathlib.Path("/opt/services/ground_truth.json"),
    secret_path: pathlib.Path = SECRET_FILE,
) -> None:
    for suf in ("", "-wal", "-shm"):
        p = pathlib.Path(f"{db_path}{suf}")
        if p.exists():
            p.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secrets.token_hex(24), encoding="utf-8")
    os.chmod(secret_path, 0o600)

    conn = get_conn(db_path)
    init_schema(conn)

    for r in RACK_SPECS:
        conn.execute(
            "INSERT INTO rack_specs (rack, spine, zone, has_rdma, cap_kw, protected) VALUES (?, ?, ?, ?, ?, ?)",
            (r["rack"], r["spine"], r["zone"], 1 if r["has_rdma"] else 0, r["cap_kw"], 1 if r["protected"] else 0),
        )

    # Seed ONLY protected R0 online inference; R1..R5 start unscheduled (or clean slate for rebalance)
    for b in range(4):
        conn.execute(
            "INSERT INTO allocations (rack, bay, shift, team, rdma, priority_iops) VALUES ('R0', ?, '24H', 'PROD-ONLINE-INFERENCE', 0, 0)",
            (b,),
        )

    tickets = [
        ("T1", "Foundation-Pretrain", "HIGH", 5.5, "AllReduce-Heavy", 30.0, 6, 6, "24H", 0, 0, 0, 0, "OPEN",
         "申请 24 卡（6 槽位 24H）用于大模型预训练主线；当前机柜池无明确减卡承诺。"),
        ("T2", "Realtime-Search-Rank", "MED", 4.2, "Zone-A NVMe + High-IO", 22.0, 4, 4, "24H", 0, 0, 0, 0, "OPEN",
         "实时搜推精排在线训练，需 16 卡（4 槽位 24H），对本地 NVMe IO 延迟极度敏感。"),
        ("T3", "Multimodal-PostTrain", "HIGH", 5.5, "AllReduce-Heavy", 20.0, 4, 4, "24H", 0, 0, 0, 0, "OPEN",
         "多模态后训练，申请 16 卡（4 槽位 24H）；跨交换机通信敏感。"),
        ("T4", "Risk-Eval-Sweep", "MED", 4.2, "High-IO Batch", 12.0, 4, 4, "24H", 0, 0, 0, 0, "OPEN",
         "风控历史样本回测扫描，申请 16 卡（4 槽位 24H），存在高频随机读写特征。"),
        ("T5", "Sales-Copilot-Tuning", "LOW", 3.0, "Interactive Tuning", 10.0, 4, 4, "24H", 0, 0, 0, 0, "OPEN",
         "算法工程微调实验，默认申请 16 卡（4 槽位 24H），具体研发作息可进一步沟通。"),
        ("T6", "Safety-RedTeam-RL", "HIGH", 5.5, "Standalone High-TDP", 6.0, 3, 3, "24H", 0, 0, 0, 0, "OPEN",
         "安全对齐红队强化学习，需 12 卡（3 槽位 24H），独立单卡高功耗持续压测。"),
        ("T7", "Marketing-GenAI-PoC", "LOW", 3.0, "PoC", 0.0, 4, 4, "24H", 0, 0, 0, 0, "OPEN",
         "市场部生成式创意试点，口头标为 P1 紧急，申请 16 卡（4 槽位 24H）。"),
        ("T8", "Legacy-OCR-Batch", "LOW", 3.0, "Legacy Batch", 0.0, 4, 4, "24H", 0, 0, 0, 0, "OPEN",
         "历史 OCR 离线批量识别扩容，申请 16 卡（4 槽位 24H）。"),
    ]
    for row in tickets:
        conn.execute(
            """INSERT INTO team_tickets (
                team_id, team_name, tdp_class, tdp_kw, workload_profile, weight,
                initial_req_bays, req_bays, accepted_shift,
                require_rdma, require_single_spine, require_no_bleed, require_priority_iops,
                status, summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )

    pos = [
        ("T1", "PO-2026-Q4-PRE", "ACTIVE", 35000.0, "", "Approved core pretraining budget."),
        ("T2", "PO-2026-Q4-SRC", "ACTIVE", 32000.0, "", "Approved search ranking budget."),
        ("T3", "PO-2026-Q4-MM", "ACTIVE", 26000.0, "", "Approved multimodal post-train budget."),
        ("T4", "PO-2026-Q4-RSK", "ACTIVE", 20000.0, "", "Approved risk evaluation budget."),
        ("T5", "PO-2026-Q4-SLS", "ACTIVE", 18000.0, "", "Approved engineering tuning budget."),
        ("T6", "PO-2026-Q4-SEC", "ACTIVE", 22000.0, "", "Approved safety redteam budget."),
        ("T7", "PO-2026-Q4-MKT", "FROZEN_EXPIRED", 0.0, "", "Q4 Marketing PoC budget frozen/expired on 2026-11-15; no production H100 quota."),
        ("T8", "PO-2026-Q4-OCR", "ACTIVE", 8000.0, "REQ-2026-089", "Check Team Desk historical archive REQ-2026-089 for legacy cluster overlap before allocating H100."),
    ]
    for row in pos:
        conn.execute(
            "INSERT INTO finops_po (team_id, po_code, po_status, approved_budget_usd, legacy_ref, notes) VALUES (?, ?, ?, ?, ?, ?)",
            row,
        )

    conn.commit()
    conn.close()

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, baseline_path)
    os.chmod(baseline_path, 0o600)

    truth = {
        "task_id": "infra-capacity/gpu-cluster-rebalance",
        "task_version": "0.6.0",
        "protected_rack": "R0",
        "protected_team": "PROD-ONLINE-INFERENCE",
        "valid_teams": ["T1", "T2", "T3", "T4", "T5", "T6"],
        "decoy_teams": ["T7", "T8"],
        "max_sim_runs": 6,
        "budget_cap_usd": 138000.0,
        "scoring": {"u_greedy": 78.0, "u_star": 99.5, "exp": 2.2, "signoff_weight": 0.15, "opt_weight": 0.85},
    }
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    truth_path.write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(truth_path, 0o600)


if __name__ == "__main__":
    seed_database()
    print("SEED_DB_OK")
