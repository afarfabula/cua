import json
import os
import pathlib
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SIM_C = REPO_ROOT / "environment" / "engine" / "sim_engine.c"


@pytest.fixture(scope="module")
def sim_bin(tmp_path_factory):
    out = tmp_path_factory.mktemp("bin") / "sim_engine"
    subprocess.run(
        ["gcc", "-O2", "-Wall", "-Wextra", "-s", str(SIM_C), "-lm", "-o", str(out)],
        check=True,
    )
    return str(out)


def run_engine(bin_path: str, assigns: list, neg: dict) -> dict:
    r0 = [{"rack": "R0", "bay": b, "shift": "24H", "team": "PROD-ONLINE-INFERENCE", "rdma": False, "priority_iops": False} for b in range(4)]
    raw = json.dumps({"assignments": r0 + assigns, "negotiated": neg})
    out = subprocess.check_output([bin_path], input=raw, text=True)
    return json.loads(out)


def convex_reward(sim_res: dict, s_signoff: float) -> float:
    g = 1.0 if sim_res["hard_feasible"] else 0.0
    s_opt = max(0.0, min(1.0, (sim_res["u_total"] - 78.0) / (99.5 - 78.0))) ** 2.2
    return round(g * (0.15 * s_signoff + 0.85 * s_opt), 4)


def test_nop_zero(sim_bin):
    res = run_engine(sim_bin, [], {})
    assert res["hard_feasible"] is True
    assert convex_reward(res, 0.0) == 0.0


def test_greedy_and_partial_ceilings(sim_bin):
    greedy_assigns = (
        [{"rack": "R4", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in range(4)]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in range(4)]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False} for b in range(3)]
        + [{"rack": "R2", "bay": 3, "shift": "24H", "team": "T4", "rdma": False, "priority_iops": True}]
        + [{"rack": "R3", "bay": b, "shift": "24H", "team": "T4", "rdma": False, "priority_iops": True} for b in range(3)]
        + [{"rack": "R5", "bay": b, "shift": "24H", "team": "T5", "rdma": False, "priority_iops": False} for b in range(4)]
    )
    greedy_neg = {"T1": {"req_bays": 4, "shift": "24H", "require_rdma": True}, "T3": {"req_bays": 3, "shift": "24H"}}
    r_greedy = run_engine(sim_bin, greedy_assigns, greedy_neg)
    assert r_greedy["status"] == "FEASIBLE"
    assert convex_reward(r_greedy, 3 / 6) <= 0.20

    full_neg = {
        "T1": {"req_bays": 4, "shift": "24H", "require_rdma": True},
        "T3": {"req_bays": 3, "shift": "24H", "require_single_spine": True, "require_no_bleed": True},
        "T4": {"req_bays": 4, "shift": "NIGHT_ONLY", "require_priority_iops": True},
        "T5": {"req_bays": 4, "shift": "DAY_ONLY"},
    }
    part_b = (
        [{"rack": "R4", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in range(4)]
        + [{"rack": "R1", "bay": 0, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False},
           {"rack": "R1", "bay": 1, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False},
           {"rack": "R2", "bay": 0, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False}]
        + [{"rack": "R5", "bay": b, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False} for b in range(3)]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (2, 3)]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (2, 3)]
        + [{"rack": "R3", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in range(4)]
        + [{"rack": "R3", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in range(4)]
    )
    r_part_b = run_engine(sim_bin, part_b, full_neg)
    assert r_part_b["status"] == "FEASIBLE"
    assert convex_reward(r_part_b, 5 / 6) <= 0.30


def test_oracle_and_symmetry(sim_bin):
    full_neg = {
        "T1": {"req_bays": 4, "shift": "24H", "require_rdma": True},
        "T3": {"req_bays": 3, "shift": "24H", "require_single_spine": True, "require_no_bleed": True},
        "T4": {"req_bays": 4, "shift": "NIGHT_ONLY", "require_priority_iops": True},
        "T5": {"req_bays": 4, "shift": "DAY_ONLY"},
    }
    oracle_a = (
        [{"rack": "R4", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R5", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R3", "bay": 0, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False}]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R3", "bay": 2, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False}]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R3", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in (1, 3)]
        + [{"rack": "R4", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in (1, 3)]
        + [{"rack": "R3", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R4", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in (1, 3)]
    )
    sym_a = (
        [{"rack": "R4", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R5", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R3", "bay": 1, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False}]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R3", "bay": 3, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False}]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R4", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in (0, 2)]
        + [{"rack": "R5", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in (0, 2)]
        + [{"rack": "R4", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R5", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in (0, 2)]
    )
    for layout in (oracle_a, sym_a):
        res = run_engine(sim_bin, layout, full_neg)
        assert res["u_total"] == 100.0
        assert convex_reward(res, 1.0) == 1.0


def test_autobu_thermal_bleed_and_zero_cost(sim_bin):
    full_neg = {
        "T1": {"req_bays": 4, "shift": "24H", "require_rdma": True},
        "T3": {"req_bays": 3, "shift": "24H", "require_single_spine": True, "require_no_bleed": True},
        "T4": {"req_bays": 4, "shift": "NIGHT_ONLY", "require_priority_iops": True},
        "T5": {"req_bays": 4, "shift": "DAY_ONLY"},
    }
    oracle_base = (
        [{"rack": "R4", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R5", "bay": b, "shift": "24H", "team": "T1", "rdma": True, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R3", "bay": 0, "shift": "24H", "team": "T3", "rdma": False, "priority_iops": False}]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False} for b in (0, 2)]
        + [{"rack": "R3", "bay": 2, "shift": "24H", "team": "T6", "rdma": False, "priority_iops": False}]
        + [{"rack": "R1", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R2", "bay": b, "shift": "24H", "team": "T2", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R3", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in (1, 3)]
        + [{"rack": "R4", "bay": b, "shift": "NIGHT_ONLY", "team": "T4", "rdma": False, "priority_iops": True} for b in (1, 3)]
        + [{"rack": "R3", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in (1, 3)]
        + [{"rack": "R4", "bay": b, "shift": "DAY_ONLY", "team": "T5", "rdma": False, "priority_iops": False} for b in (1, 3)]
    )
    # Placing SHARED-AUTO-BU-HIGH at R5 Bay 1 triggers -28% vertical bleed on adjacent T1 (R5 Bay 0, 2) + +5.5kW power, $0 FinOps cost
    with_high = oracle_base + [{"rack": "R5", "bay": 1, "shift": "24H", "team": "SHARED-AUTO-BU-HIGH", "rdma": False, "priority_iops": False}]
    res = run_engine(sim_bin, with_high, full_neg)
    assert res["hard_feasible"] is True
    assert res["monthly_cost_usd"] == 113200.0
    assert res["teams"]["T1"]["thermal_bleed"] is True
    assert round(res["teams"]["T1"]["efficiency"], 4) == 0.72
    r5 = [r for r in res["racks"] if r["rack"] == "R5"][0]
    assert r5["peak_kw"] == 16.5


def test_no_forbidden_bans_or_autobu_spoilers_in_docs():
    banned = ["只能用 UI", "只能用UI", "禁止调用接口", "禁止调用 API", "禁止调用API", "不能用 DOM", "不能用DOM", "禁止 CDP"]
    spoilers = ["AUTO-BU", "SHARED-AUTO-BU", "Adjacent-BU", "第 1 次", "第 2 次", "第 3 次", "run_id"]
    for p in [
        REPO_ROOT / "instruction.md",
        REPO_ROOT / "proposal.md",
        REPO_ROOT / "environment" / "workspace_init" / "README.md",
        REPO_ROOT / "environment" / "workspace_init" / "docs" / "cluster_capacity_policy.md",
        REPO_ROOT / "environment" / "workspace_init" / "docs" / "delivery_schema.md",
    ]:
        txt = p.read_text(encoding="utf-8")
        for b in banned:
            assert b not in txt, f"Forbidden phrase {b!r} found in {p}"
        for sp in spoilers:
            assert sp not in txt, f"Spoiler phrase {sp!r} leaked into {p}"
