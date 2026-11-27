import contextlib
import csv
import json
import os
import pathlib
import sqlite3
import sys

SERVICES_DIR = pathlib.Path(os.environ.get("SERVICES_DIR", "/opt/services/services"))
if not SERVICES_DIR.is_dir():
    SERVICES_DIR = pathlib.Path(__file__).resolve().parents[1] / "environment" / "services"
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from common import RACK_SPECS, TEAM_ORDER, call_sim_engine  # noqa: E402

CLUSTER_DB = pathlib.Path(os.environ.get("CLUSTER_DB_PATH", "/var/lib/cluster-ops/cluster.db"))
BASELINE_DB = pathlib.Path(os.environ.get("BASELINE_DB_PATH", "/var/lib/cluster-ops/baseline.db"))
TRUTH_PATH = pathlib.Path(os.environ.get("TRUTH_PATH", "/opt/services/ground_truth.json"))
SIM_ENGINE_BIN = os.environ.get("SIM_ENGINE_BIN", "/opt/services/engine/sim_engine")
OUTPUT_DIR = pathlib.Path(os.environ.get("OUTPUT_DIR", "/workspace/output"))
VERIFIER_DIR = pathlib.Path(os.environ.get("VERIFIER_DIR", "/logs/verifier"))


class InfraError(RuntimeError):
    pass


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _verify_signoff_against_layout(team_id: str, ticket: dict, sim_raw: dict, assigns: list) -> bool:
    if ticket.get("status") != "SIGNED_OFF":
        return False
    tinfo = sim_raw.get("teams", {}).get(team_id, {})
    if tinfo.get("coverage", 0.0) < 0.999:
        return False
    if team_id == "T1":
        if ticket.get("require_rdma") and not tinfo.get("all_rdma_ok"):
            return False
        if tinfo.get("cross_spine"):
            return False
    elif team_id == "T2":
        if not tinfo.get("zone_a_ok") or tinfo.get("io_contended"):
            return False
    elif team_id == "T3":
        if ticket.get("require_single_spine") and tinfo.get("cross_spine"):
            return False
        if ticket.get("require_no_bleed") and tinfo.get("thermal_bleed"):
            return False
    elif team_id == "T4":
        if ticket.get("require_priority_iops") and not tinfo.get("all_iops_ok"):
            return False
        if tinfo.get("io_contended"):
            return False
    elif team_id == "T5":
        if ticket.get("accepted_shift") == "DAY_ONLY":
            for a in assigns:
                if a["team"] == "T5" and a["shift"] == "NIGHT_ONLY":
                    return False
    return True


def _check_local_deliverables(
    output_dir: pathlib.Path,
    release_code: str,
    committed_cost: float,
    committed_assigns: list,
    committed_tickets: dict,
) -> tuple[bool, list[str]]:
    errors = []
    dec_path = output_dir / "allocation_decision.json"
    csv_path = output_dir / "cost_reconciliation.csv"
    if not dec_path.is_file():
        return False, ["missing allocation_decision.json"]
    if not csv_path.is_file():
        return False, ["missing cost_reconciliation.csv"]

    try:
        dec = json.loads(dec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"invalid allocation_decision.json: {exc}"]

    if dec.get("release_code") != release_code:
        errors.append(f"release_code mismatch: {dec.get('release_code')} != {release_code}")
    try:
        if abs(float(dec.get("monthly_cost_usd", -1.0)) - committed_cost) > 0.05:
            errors.append("monthly_cost_usd mismatch in JSON")
    except Exception:
        errors.append("invalid monthly_cost_usd in JSON")

    def norm_set(lst):
        return sorted(
            (str(x["rack"]), int(x["bay"]), str(x["shift"]), str(x["team"]), bool(x.get("rdma", False)), bool(x.get("priority_iops", False)))
            for x in lst
        )

    try:
        if norm_set(dec.get("assignments", [])) != norm_set(committed_assigns):
            errors.append("assignments mismatch in allocation_decision.json")
    except Exception:
        errors.append("malformed assignments in allocation_decision.json")

    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            expected_hdr = ["team_id", "status", "req_bays", "assigned_bays", "accepted_shift", "monthly_cost_usd"]
            if reader.fieldnames != expected_hdr:
                errors.append(f"CSV header mismatch: {reader.fieldnames}")
            rows = {r["team_id"]: r for r in reader}
    except Exception as exc:
        return False, [f"invalid cost_reconciliation.csv: {exc}"]

    if set(rows.keys()) != set(TEAM_ORDER):
        errors.append("CSV missing teams T1..T8")
    else:
        csv_sum = 0.0
        for tid in TEAM_ORDER:
            r = rows[tid]
            tk = committed_tickets.get(tid, {})
            if tid in {"T7", "T8"} and (r["status"] != "DEFERRED" or int(r["assigned_bays"]) != 0 or float(r["monthly_cost_usd"]) != 0.0):
                errors.append(f"CSV decoy {tid} not DEFERRED/0")
            if tid in {"T1", "T2", "T3", "T4", "T5", "T6"} and r["status"] != tk.get("status"):
                errors.append(f"CSV status mismatch for {tid}")
            csv_sum += float(r["monthly_cost_usd"])
        if abs(csv_sum - committed_cost) > 0.05:
            errors.append(f"CSV cost sum {csv_sum} != {committed_cost}")

    return len(errors) == 0, errors


def evaluate() -> dict:
    if not CLUSTER_DB.is_file() or not BASELINE_DB.is_file() or not TRUTH_PATH.is_file():
        raise InfraError("Missing cluster DB, baseline DB, or ground_truth.json")
    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    scoring = truth["scoring"]

    try:
        with contextlib.closing(sqlite3.connect(f"{BASELINE_DB.resolve().as_uri()}?mode=ro", uri=True)) as base:
            base.row_factory = sqlite3.Row
            base_r0 = sorted(
                (r["rack"], int(r["bay"]), r["shift"], r["team"])
                for r in base.execute("SELECT rack, bay, shift, team FROM allocations WHERE rack='R0' OR team='PROD-ONLINE-INFERENCE'")
            )
        with contextlib.closing(sqlite3.connect(f"{CLUSTER_DB.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            live_r0 = sorted(
                (r["rack"], int(r["bay"]), r["shift"], r["team"])
                for r in conn.execute("SELECT rack, bay, shift, team FROM allocations WHERE rack='R0' OR team='PROD-ONLINE-INFERENCE'")
            )
            sim_runs = int(conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0])
            live_assigns = [
                {
                    "rack": r["rack"],
                    "bay": int(r["bay"]),
                    "shift": r["shift"],
                    "team": r["team"],
                    "rdma": bool(r["rdma"]),
                    "priority_iops": bool(r["priority_iops"]),
                }
                for r in conn.execute("SELECT rack, bay, shift, team, rdma, priority_iops FROM allocations ORDER BY rack, bay, shift")
            ]
            live_tickets = {r["team_id"]: dict(r) for r in conn.execute("SELECT * FROM team_tickets")}
            auto_bu_rows = sorted(
                (r["rack"], int(r["bay"]), r["shift"], r["team"])
                for r in conn.execute("SELECT rack, bay, shift, team FROM auto_bu_claims")
            )
            commit_row = conn.execute("SELECT * FROM release_commits ORDER BY commit_id DESC LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise InfraError(f"Database read error: {exc}") from exc

    r0_intact = (base_r0 == live_r0)
    live_tuples = {(x["rack"], x["bay"], x["shift"], x["team"]) for x in live_assigns}
    auto_bu_intact = all(item in live_tuples for item in auto_bu_rows)
    quota_ok = (sim_runs <= int(truth["max_sim_runs"]))

    committed = commit_row is not None
    if committed:
        release_code = commit_row["release_code"]
        eval_assigns = json.loads(commit_row["allocations_json"])
        tickets_list = json.loads(commit_row["tickets_json"])
        eval_tickets = {t["team_id"]: t for t in tickets_list}
        live_matches_commit = (
            sorted((x["rack"], x["bay"], x["shift"], x["team"], bool(x["rdma"]), bool(x["priority_iops"])) for x in live_assigns)
            == sorted((x["rack"], int(x["bay"]), x["shift"], x["team"], bool(x["rdma"]), bool(x["priority_iops"])) for x in eval_assigns)
        )
        eval_r0 = sorted((x["rack"], int(x["bay"]), x["shift"], x["team"]) for x in eval_assigns if x["rack"] == "R0" or x["team"] == "PROD-ONLINE-INFERENCE")
        r0_intact = r0_intact and (eval_r0 == base_r0)
        eval_tuples = {(x["rack"], int(x["bay"]), x["shift"], x["team"]) for x in eval_assigns}
        auto_bu_intact = auto_bu_intact and all(item in eval_tuples for item in auto_bu_rows)
    else:
        release_code = None
        eval_assigns = live_assigns
        eval_tickets = live_tickets
        live_matches_commit = False

    neg_map = {
        tid: {
            "req_bays": int(tk["req_bays"]),
            "shift": tk["accepted_shift"],
            "require_rdma": bool(tk["require_rdma"]),
            "require_single_spine": bool(tk["require_single_spine"]),
            "require_no_bleed": bool(tk["require_no_bleed"]),
            "require_priority_iops": bool(tk["require_priority_iops"]),
        }
        for tid, tk in eval_tickets.items()
    }
    sim_raw = call_sim_engine({"assignments": eval_assigns, "negotiated": neg_map}, engine_bin=SIM_ENGINE_BIN)

    r0_intact = r0_intact and bool(sim_raw["r0_intact"])
    power_ok = bool(sim_raw["power_ok"])
    budget_ok = bool(sim_raw["budget_ok"])
    decoy_ok = all(
        eval_tickets.get(d, {}).get("status") == "DEFERRED" and sim_raw["teams"].get(d, {}).get("raw_bays", 0) == 0
        for d in truth["decoy_teams"]
    )
    local_ok, local_errs = (
        _check_local_deliverables(OUTPUT_DIR, release_code, float(sim_raw["monthly_cost_usd"]), eval_assigns, eval_tickets)
        if committed
        else (False, ["no committed release"])
    )

    g_hard = 1.0
    if not r0_intact or not auto_bu_intact:
        g_hard = 0.0
    if not quota_ok:
        g_hard = 0.0
    if not power_ok:
        g_hard *= 0.15
    if not budget_ok:
        g_hard *= 0.20
    if not committed or not live_matches_commit:
        g_hard *= 0.25
    if not decoy_ok:
        g_hard *= 0.60
    if not local_ok:
        g_hard *= 0.70

    valid_signoffs = [
        tid for tid in truth["valid_teams"]
        if _verify_signoff_against_layout(tid, eval_tickets.get(tid, {}), sim_raw, eval_assigns)
    ]
    s_signoff = len(valid_signoffs) / float(len(truth["valid_teams"]))

    u_total = float(sim_raw["u_total"])
    s_opt = clamp((u_total - scoring["u_greedy"]) / (scoring["u_star"] - scoring["u_greedy"]), 0.0, 1.0) ** scoring["exp"]
    reward = round(g_hard * (scoring["signoff_weight"] * s_signoff + scoring["opt_weight"] * s_opt), 4)

    return {
        "task_id": truth["task_id"],
        "task_version": truth["task_version"],
        "reward": reward,
        "gates": {
            "g_hard": round(g_hard, 4),
            "r0_intact": r0_intact,
            "auto_bu_intact": auto_bu_intact,
            "auto_bu_claims_count": len(auto_bu_rows),
            "quota_ok": quota_ok,
            "sim_runs_used": sim_runs,
            "power_ok": power_ok,
            "budget_ok": budget_ok,
            "committed": committed,
            "live_matches_commit": live_matches_commit,
            "decoy_ok": decoy_ok,
            "local_ok": local_ok,
            "local_errors": local_errs,
        },
        "scores": {
            "s_signoff": round(s_signoff, 4),
            "valid_signoff_teams": valid_signoffs,
            "u_raw": sim_raw["u_raw"],
            "cost_eff": sim_raw["cost_eff"],
            "u_total": u_total,
            "monthly_cost_usd": sim_raw["monthly_cost_usd"],
            "s_opt": round(s_opt, 4),
        },
    }


def main() -> int:
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    report = evaluate()
    (VERIFIER_DIR / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (VERIFIER_DIR / "reward.txt").write_text(f"{report['reward']:.4f}\n", encoding="utf-8")
    print(f"REWARD={report['reward']:.4f} U_TOTAL={report['scores']['u_total']:.2f} G_HARD={report['gates']['g_hard']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
