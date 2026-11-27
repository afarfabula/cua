import csv
import json
import os
import sys

import requests

RACK_URL = os.environ.get("RACK_CONSOLE_URL", "http://127.0.0.1:8081")
TEAM_URL = os.environ.get("TEAM_DESK_URL", "http://127.0.0.1:8082")
FINOPS_URL = os.environ.get("FINOPS_URL", "http://127.0.0.1:8083")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/workspace/output")
USER = "cap_lead"
PASS = "Cluster!2026"


def login_session(base_url: str) -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{base_url}/login",
        data={"username": USER, "password": PASS},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return s


def main() -> None:
    s_rack = login_session(RACK_URL)
    s_team = login_session(TEAM_URL)
    s_fin = login_session(FINOPS_URL)

    # 1. Inspect FinOps ledger & Team Archive to identify decoys T7 and T8
    ledger = s_fin.get(f"{FINOPS_URL}/api/finops/ledger", timeout=15).json()
    tickets_data = s_team.get(f"{TEAM_URL}/api/tickets", timeout=15).json()
    po_map = {p["team_id"]: p for p in ledger["po_ledger"]}
    dup_teams = {a["team_id"] for a in tickets_data["archive"] if a["status"] == "ALREADY_FULFILLED_ON_L40_POOL"}

    deferred = []
    for tid in ["T7", "T8"]:
        if po_map[tid]["po_status"] != "ACTIVE" or tid in dup_teams:
            s_team.post(
                f"{TEAM_URL}/api/tickets/{tid}/defer",
                json={"reason": f"Ineligible PO={po_map[tid]['po_status']} or duplicate L40 archive"},
                timeout=15,
            ).raise_for_status()
            deferred.append(tid)

    # 2. Negotiate concessions & clarify constraints
    s_team.post(f"{TEAM_URL}/api/tickets/T1/negotiate", json={"message": "提供 Spine-B RDMA 专网，缩减至 16 卡 (4 bays 24H)", "offered_gpus": 16, "rdma": True}, timeout=15).raise_for_status()
    s_team.post(f"{TEAM_URL}/api/tickets/T2/negotiate", json={"message": "确认 Zone-A 本地 NVMe 与独立机柜 IO 要求"}, timeout=15).raise_for_status()
    s_team.post(f"{TEAM_URL}/api/tickets/T3/negotiate", json={"message": "保证单 Spine 部署且无相邻高功耗热降频，压缩至 12 卡 (3 bays)", "offered_gpus": 12, "single_spine": True, "no_thermal_bleed": True}, timeout=15).raise_for_status()
    s_team.post(f"{TEAM_URL}/api/tickets/T4/negotiate", json={"message": "开启 priority_iops 并切至 NIGHT_ONLY (21:00-09:00) 4 槽位", "offered_bays": 4, "shift": "NIGHT_ONLY", "priority_iops": True}, timeout=15).raise_for_status()
    s_team.post(f"{TEAM_URL}/api/tickets/T5/negotiate", json={"message": "提供 DAY_ONLY (09:00-21:00) 4 槽位共享错峰", "offered_bays": 4, "shift": "DAY_ONLY"}, timeout=15).raise_for_status()

    # 3. Preserve R0 and write Pareto-optimal checkerboard + time-shared layout across R1..R5
    cluster = s_rack.get(f"{RACK_URL}/api/cluster", timeout=15).json()
    r0_assigns = [a for a in cluster["assignments"] if a["rack"] == "R0"]

    optimal_assigns = r0_assigns + [
        # T1 (4 bays, 24H, RDMA on Spine-B R4/R5, non-adjacent B0/B2)
        {"rack": "R4", "bay": 0, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False},
        {"rack": "R4", "bay": 2, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False},
        {"rack": "R5", "bay": 0, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False},
        {"rack": "R5", "bay": 2, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False},
        # T3 (3 bays, 24H, single Spine-A R1/R3, non-adjacent B0/B2 & B0)
        {"rack": "R1", "bay": 0, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False},
        {"rack": "R1", "bay": 2, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False},
        {"rack": "R3", "bay": 0, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False},
        # T6 (3 bays, 24H, R2/R3, non-adjacent B0/B2 & B2)
        {"rack": "R2", "bay": 0, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False},
        {"rack": "R2", "bay": 2, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False},
        {"rack": "R3", "bay": 2, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False},
        # T2 (4 bays, 24H, strictly Zone-A R1/R2 B1/B3, zero T4 sharing)
        {"rack": "R1", "bay": 1, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False},
        {"rack": "R1", "bay": 3, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False},
        {"rack": "R2", "bay": 1, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False},
        {"rack": "R2", "bay": 3, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False},
        # T4 NIGHT_ONLY + priority_iops shared with T5 DAY_ONLY on R3/R4 B1/B3
        {"rack": "R3", "bay": 1, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True},
        {"rack": "R3", "bay": 3, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True},
        {"rack": "R4", "bay": 1, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True},
        {"rack": "R4", "bay": 3, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True},
        {"rack": "R3", "bay": 1, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False},
        {"rack": "R3", "bay": 3, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False},
        {"rack": "R4", "bay": 1, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False},
        {"rack": "R4", "bay": 3, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False},
    ]

    s_rack.post(f"{RACK_URL}/api/allocations", json={"assignments": optimal_assigns}, timeout=15).raise_for_status()
    sim = s_rack.post(f"{RACK_URL}/api/simulations/run", timeout=15).json()
    assert sim["hard_feasible"] and sim["effective_cluster_utility"] >= 99.5, sim

    hm = s_rack.get(f"{RACK_URL}/api/simulations/heatmap.png", timeout=15)
    assert hm.status_code == 200 and hm.content[:8] == b"\x89PNG\r\n\x1a\n"

    # 4. Obtain all 6 valid team sign-offs
    signed = []
    for tid in ["T1", "T2", "T3", "T4", "T5", "T6"]:
        res = s_team.post(f"{TEAM_URL}/api/tickets/{tid}/signoff", timeout=15).json()
        assert res["ok"], f"Signoff failed for {tid}: {res}"
        signed.append(tid)

    # 5. Commit release on FinOps & write output deliverables
    rel = s_fin.post(f"{FINOPS_URL}/api/release/commit", timeout=15).json()
    assert rel["ok"], rel

    final_ledger = s_fin.get(f"{FINOPS_URL}/api/finops/ledger", timeout=15).json()
    final_tickets = {t["team_id"]: t for t in s_team.get(f"{TEAM_URL}/api/tickets", timeout=15).json()["tickets"]}
    by_team_cost = final_ledger["draft_cost"]["by_team"]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    decision = {
        "release_code": rel["release_code"],
        "monthly_cost_usd": rel["total_monthly_cost_usd"],
        "effective_cluster_utility": sim["effective_cluster_utility"],
        "simulations_used": sim["runs_used"],
        "deferred_teams": deferred,
        "signed_off_teams": signed,
        "assignments": [
            {k: a[k] for k in ("rack", "bay", "shift", "team", "rdma", "priority_iops")}
            for a in s_rack.get(f"{RACK_URL}/api/allocations", timeout=15).json()["assignments"]
        ],
    }
    with open(os.path.join(OUTPUT_DIR, "allocation_decision.json"), "w", encoding="utf-8") as f:
        json.dump(decision, f, indent=2, ensure_ascii=False)

    with open(os.path.join(OUTPUT_DIR, "cost_reconciliation.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["team_id", "status", "req_bays", "assigned_bays", "accepted_shift", "monthly_cost_usd"])
        for tid in ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]:
            tk = final_tickets[tid]
            tc = by_team_cost.get(tid, {"bays": 0, "total_cost_usd": 0.0})
            w.writerow([tid, tk["status"], tk["req_bays"], tc["bays"], tk["accepted_shift"], f"{tc['total_cost_usd']:.2f}"])

    print(f"ORACLE_DONE release={rel['release_code']} cost={rel['total_monthly_cost_usd']} util={sim['effective_cluster_utility']}")


if __name__ == "__main__":
    main()
