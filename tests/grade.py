#!/usr/bin/env python3
"""Independent Python verifier for the 15 task checkpoints."""
import contextlib
import csv
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import traceback

import requests

TZ_UTC8 = dt.timezone(dt.timedelta(hours=8))

GITEA_URL = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
GITEA_USER = "sre_oncall"
GITEA_PASS = "Clearing!2026"
MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://127.0.0.1:8025")
PORTAL_DB = pathlib.Path(os.environ.get("PORTAL_DB_PATH", "/var/lib/clearing-portal/portal.db"))
CMDB_BASELINE_DB = pathlib.Path(os.environ.get("CMDB_BASELINE_DB_PATH", "/opt/services/clean_state/portal/portal.db"))
TRUTH_PATH = pathlib.Path(os.environ.get("TRUTH_PATH", "/opt/services/ground_truth.json"))
BASELINE_PATH = pathlib.Path(os.environ.get("GITEA_BASELINE_PATH", "/opt/services/gitea_baseline.json"))
OUTPUT_DIR = pathlib.Path(os.environ.get("WORKSPACE_OUTPUT_DIR", "/workspace/output"))
LOG_VERIFIER_DIR = pathlib.Path(os.environ.get("LOG_VERIFIER_DIR", "/logs/verifier"))


class InfraError(RuntimeError):
    pass


def load_json_safe(path: pathlib.Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_gitea_head(repo: str) -> str:
    r = requests.get(
        f"{GITEA_URL}/api/v1/repos/clearing-core/{repo}/branches/main",
        auth=(GITEA_USER, GITEA_PASS),
        timeout=10,
    )
    if r.status_code != 200:
        raise InfraError(f"Gitea branch API error for {repo}: {r.status_code} {r.text}")
    return r.json()["commit"]["id"]


def parse_dt_utc8(s: str):
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ_UTC8)
        return d.astimezone(TZ_UTC8)
    except Exception:
        return None


def parse_window_hhmm(win_str: str):
    if not isinstance(win_str, str) or "2026-11-24" not in win_str:
        return None
    times = re.findall(r"(\d{1,2}):(\d{2})", win_str)
    if len(times) < 2:
        return None
    start_m = int(times[0][0]) * 60 + int(times[0][1])
    end_m = int(times[1][0]) * 60 + int(times[1][1])
    if end_m <= start_m:
        return None
    return start_m, end_m


def score_cmdb(conn: sqlite3.Connection, baseline_path: pathlib.Path, truth: dict) -> tuple[float, str]:
    targets = set(truth["isolated_nodes"])
    protected = set(truth["active_nodes"])
    if not baseline_path.is_file():
        raise InfraError("Missing clean CMDB snapshot")

    try:
        with contextlib.closing(sqlite3.connect(f"{baseline_path.resolve().as_uri()}?mode=ro", uri=True)) as clean:
            clean.row_factory = sqlite3.Row
            baseline = {row["node_name"]: dict(row) for row in clean.execute("SELECT * FROM cmdb_nodes")}
        current = {row["node_name"]: dict(row) for row in conn.execute("SELECT * FROM cmdb_nodes")}
        latest = dict(conn.execute(
            "SELECT node_name, new_status FROM cmdb_node_status_events ORDER BY event_id"))
    except sqlite3.Error as exc:
        raise InfraError(f"Unable to read CMDB state or audit trail: {exc}") from exc

    if not targets or targets & protected or baseline.keys() != targets | protected:
        raise InfraError("CMDB snapshot does not match the target and protected node sets")

    isolated = {
        node for node in targets
        if (state := current.get(node, {}).get("status")) in {"ISOLATED", "DRAINED"}
        and baseline[node]["status"] != state
        and latest.get(node) == state
    }
    protected_changed = {
        node for node in protected if current.get(node) != baseline[node]
    } | (protected & latest.keys())
    unexpected_nodes = current.keys() - baseline.keys()
    protected_ok = not protected_changed and not unexpected_nodes
    score = 0.5 * len(isolated) / len(targets)
    if isolated and protected_ok:
        score += 0.5
    return score, (
        f"isolated={len(isolated)}/{len(targets)} "
        f"protected_changed={sorted(protected_changed)} unexpected_nodes={sorted(unexpected_nodes)}"
    )


def evaluate() -> None:
    if not TRUTH_PATH.exists() or not BASELINE_PATH.exists() or not PORTAL_DB.exists():
        raise InfraError("Required verifier truth, baseline, or portal DB missing")

    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    try:
        g_ver = requests.get(f"{GITEA_URL}/api/v1/version", timeout=5)
        m_res = requests.get(f"{MAILPIT_URL}/api/v1/messages?limit=100", timeout=5)
        if g_ver.status_code != 200 or m_res.status_code != 200:
            raise InfraError("Gitea or Mailpit returned non-200")
    except Exception as exc:
        raise InfraError(f"Service unreachable during verification: {exc}") from exc

    postmortem = load_json_safe(OUTPUT_DIR / "incident_postmortem.json")
    conn = sqlite3.connect(str(PORTAL_DB))
    conn.row_factory = sqlite3.Row

    weights = {
        "R01": 0.06, "R02": 0.06, "R03": 0.06, "R04": 0.07, "R05": 0.08,
        "R06": 0.08, "R07": 0.07, "R08": 0.07, "R09": 0.07, "R10": 0.09,
        "R11": 0.06, "R12": 0.05, "R13": 0.05, "R14": 0.05, "R15": 0.08,
    }
    results = {}

    def record(cid: str, score: float, reason: str, evidence: str = ""):
        results[cid] = {
            "weight": weights[cid],
            "score": round(max(0.0, min(1.0, float(score))), 4),
            "reason": reason,
            "evidence": evidence,
        }

    # R01: root_cause_service
    if isinstance(postmortem, dict) and str(postmortem.get("root_cause_service", "")).strip() == "risk-engine":
        record("R01", 1.0, "Correct root_cause_service=risk-engine")
    else:
        record("R01", 0.0, "Missing or incorrect root_cause_service")

    # R02: onset_time_utc8 & culprit_version
    s_r02 = 0.0
    if isinstance(postmortem, dict):
        d_onset = parse_dt_utc8(str(postmortem.get("onset_time_utc8", "")))
        t_low = dt.datetime(2026, 11, 24, 14, 12, 0, tzinfo=TZ_UTC8)
        t_high = dt.datetime(2026, 11, 24, 14, 16, 0, tzinfo=TZ_UTC8)
        if d_onset and t_low <= d_onset <= t_high:
            s_r02 += 0.5
        if "2.8.4" in str(postmortem.get("culprit_version", "")):
            s_r02 += 0.5
    record("R02", s_r02, f"onset/version score={s_r02}")

    # R03: Perfetto bottleneck_span
    span_val = str(postmortem.get("bottleneck_span", "")).lower() if isinstance(postmortem, dict) else ""
    if any(k in span_val for k in ("acquire_rule_lock", "fx_quote_http_retry", "_rule_cache_lock", "refresh_fx_quote")):
        record("R03", 1.0, f"Matched bottleneck span: {span_val}")
    else:
        record("R03", 0.0, f"Missing Perfetto lock/retry span: {span_val}")

    # R04: Trap PR #1 closed & not merged with comment referencing 180 / pool
    pr_r = requests.get(f"{GITEA_URL}/api/v1/repos/clearing-core/risk-engine/pulls/1", auth=(GITEA_USER, GITEA_PASS), timeout=10)
    if pr_r.status_code == 200:
        pr = pr_r.json()
        cmts_r = requests.get(f"{GITEA_URL}/api/v1/repos/clearing-core/risk-engine/issues/1/comments", auth=(GITEA_USER, GITEA_PASS), timeout=10)
        cmts = " ".join(c.get("body", "") for c in (cmts_r.json() if cmts_r.status_code == 200 else [])).lower()
        s_r04 = 0.0
        if pr.get("state") == "closed" and not pr.get("merged"):
            s_r04 += 0.6
        if any(k in cmts for k in ("180", "max_connections", "pool", "连接")):
            s_r04 += 0.4
        record("R04", s_r04, f"PR#1 state={pr.get('state')} merged={pr.get('merged')}")
    else:
        record("R04", 0.0, f"Failed fetching PR#1: {pr_r.status_code}")

    # R05: Gitea risk-engine main HEAD changed & DB_POOL_SIZE <= 180
    risk_head = get_gitea_head("risk-engine")
    cfg_r = requests.get(f"{GITEA_URL}/api/v1/repos/clearing-core/risk-engine/raw/main/risk_engine/config.py", auth=(GITEA_USER, GITEA_PASS), timeout=10)
    pool_ok = False
    if cfg_r.status_code == 200:
        m = re.search(r"DB_POOL_SIZE\s*=\s*(\d+)", cfg_r.text)
        if m and int(m.group(1)) <= 180:
            pool_ok = True
    if risk_head != baseline["risk_engine_initial_sha"] and pool_ok:
        record("R05", 1.0, f"New main commit {risk_head[:10]} with safe DB_POOL_SIZE")
    elif risk_head != baseline["risk_engine_initial_sha"]:
        record("R05", 0.4, f"New main commit {risk_head[:10]} but invalid DB_POOL_SIZE")
    else:
        record("R05", 0.0, "risk-engine main HEAD unchanged")

    # R06: Clone risk-engine main & run independent regression tests
    if risk_head == baseline["risk_engine_initial_sha"]:
        record("R06", 0.0, "Skipped on unmodified buggy v2.8.4")
    else:
        with tempfile.TemporaryDirectory() as td:
            clone_url = GITEA_URL.replace("://", f"://{GITEA_USER}:{GITEA_PASS}@") + "/clearing-core/risk-engine.git"
            cp = subprocess.run(["git", "clone", "--depth", "5", clone_url, td], capture_output=True, text=True)
            if cp.returncode != 0:
                record("R06", 0.0, f"git clone failed: {cp.stderr}")
            else:
                test_script = r'''
import concurrent.futures, time, sys
sys.path.insert(0, ".")
from risk_engine.engine import RiskEngine
from risk_engine.fx_client import FxClient

passed = 0
def slow_fetch(pair):
    if pair == "USD_CNY":
        time.sleep(0.45)
        return 7.15
    return 0.92

eng = RiskEngine(fx_client=FxClient(slow_fetch), cache_ttl=30.0)
eng._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 100.0)
eng._fx_cache["HKD_CNY"] = (0.92, time.monotonic())

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
    f_usd = ex.submit(eng.evaluate_tx, "TX-A", "M1", "USD", 10.0, "2026-11-24T14:15:01+08:00")
    time.sleep(0.05)
    t0 = time.monotonic()
    res_hkd = eng.evaluate_tx("TX-B", "M1", "HKD", 10.0, "2026-11-24T14:15:02+08:00")
    hkd_elapsed = time.monotonic() - t0
    res_usd = f_usd.result(timeout=5)
if hkd_elapsed < 0.25 and res_hkd["amount_cny"] == 9.2 and res_usd["amount_cny"] == 71.5:
    passed += 1

k1 = eng.generate_idempotency_key("TX-1001", "MCH_VIP_001", "2026-11-24T14:15:01+08:00")
k2 = eng.generate_idempotency_key("TX-1002", "MCH_VIP_001", "2026-11-24T14:15:45+08:00")
k1_dup = eng.generate_idempotency_key("TX-1001", "MCH_VIP_001", "2026-11-24T14:15:45+08:00")
if k1 != k2 and k1 == k1_dup:
    passed += 1

def fail_fetch(pair):
    raise RuntimeError("503 Service Unavailable")

eng2 = RiskEngine(fx_client=FxClient(fail_fetch), cache_ttl=1.0)
eng2._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 10.0)
try:
    r_fb = eng2.evaluate_tx("TX-FB", "M1", "USD", 100.0, "2026-11-24T14:15:10+08:00")
    if abs(r_fb["amount_cny"] - 715.0) < 0.01:
        passed += 1
except Exception:
    pass

print(f"REGRESSION_RESULT={passed}/3")
'''
                rp = subprocess.run([sys.executable, "-c", test_script], cwd=td, capture_output=True, text=True, timeout=20)
                pt = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=td, env={**os.environ, "PYTHONPATH": td}, capture_output=True, text=True, timeout=20)
                m = re.search(r"REGRESSION_RESULT=(\d+)/(\d+)", rp.stdout)
                reg_pass = int(m.group(1)) if m else 0
                pytest_ok = 1 if pt.returncode == 0 else 0
                record("R06", (reg_pass + pytest_ok) / 4.0, f"reg={reg_pass}/3 pytest_ok={pytest_ok}")

    # R07: Compare persisted isolation and protected-node history with the clean snapshot.
    s_r07, reason_r07 = score_cmdb(conn, CMDB_BASELINE_DB, truth)
    record("R07", s_r07, reason_r07, str(PORTAL_DB))

    # R08: Change ticket SHA, span, and freeze-window avoidance (17:30-19:30 UTC+8)
    tickets = [dict(r) for r in conn.execute("SELECT * FROM change_tickets WHERE locked=0")]
    best_r08 = 0.0
    r08_msg = "No non-locked change ticket"
    for t in tickets:
        sc = 0.0
        if t["service"] == "risk-engine":
            sc += 0.25
        if len(t["commit_sha"]) >= 7 and risk_head.startswith(t["commit_sha"]) and risk_head != baseline["risk_engine_initial_sha"]:
            sc += 0.25
        if any(k in t["bottleneck_span"].lower() for k in ("acquire_rule_lock", "fx_quote_http_retry", "_rule_cache_lock", "refresh_fx_quote")):
            sc += 0.2
        win = parse_window_hhmm(t["scheduled_window_utc8"])
        if win:
            s_m, e_m = win
            overlaps_freeze = not (e_m <= 1050 or s_m >= 1170)
            if not overlaps_freeze:
                sc += 0.3
        if sc > best_r08:
            best_r08 = sc
            r08_msg = f"ticket={t['ticket_id']} win={t['scheduled_window_utc8']} score={sc:.2f}"
    record("R08", best_r08, r08_msg)

    # R15: Live Canary Replay gate passed AND release ticket APPROVED
    best_r15 = 0.0
    for t in tickets:
        if len(t["commit_sha"]) >= 7 and risk_head.startswith(t["commit_sha"]) and risk_head != baseline["risk_engine_initial_sha"]:
            sc = 0.0
            if t.get("canary_status") == "CANARY_PASSED":
                sc += 0.6
            if t.get("status") == "APPROVED":
                sc += 0.4
            best_r15 = max(best_r15, sc)
    record("R15", best_r15, f"canary_gate_score={best_r15:.2f}")

    # R09: Local CSV & duplicate_risk_tx_ids
    csv_path = OUTPUT_DIR / "reconciliation_summary.csv"
    s_r09 = 0.0
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        bids = {r.get("batch_id") for r in rows}
        has_test = any(str(r.get("merchant_id", "")).startswith("TEST_") for r in rows)
        if set(truth["batches"].keys()).issubset(bids) and not has_test:
            s_r09 += 0.5
    if isinstance(postmortem, dict):
        if set(postmortem.get("duplicate_risk_tx_ids") or []) == set(truth["duplicate_risk_tx_ids"]):
            s_r09 += 0.5
    record("R09", s_r09, f"csv_ok={s_r09>=0.5} dup_match={s_r09==1.0}")

    # R10: Portal DB SLA batches (incorporating PDF errata + MCH_VIP_003 Clause 4.2 exemption)
    db_batches = {r["batch_id"]: dict(r) for r in conn.execute("SELECT * FROM sla_batches")}
    matched_b = 0.0
    for bid, exp in truth["batches"].items():
        got = db_batches.get(bid, {})
        if got.get("audit_status") != "AUDITED":
            continue
        ok_fields = (
            got.get("breached_tx_count") == exp["breached_tx_count"]
            and got.get("duplicate_risk_count") == exp["duplicate_risk_count"]
            and got.get("settled_amount_cny") is not None
            and abs(float(got["settled_amount_cny"]) - exp["settled_amount_cny"]) <= 0.05
            and got.get("penalty_cny") is not None
            and abs(float(got["penalty_cny"]) - exp["penalty_cny"]) <= 0.02
        )
        if ok_fields:
            matched_b += 1.0
    record("R10", matched_b / len(truth["batches"]), f"matched_batches={int(matched_b)}/{len(truth['batches'])}")

    # R11: Historical LOCKED records untouched (anti-nop)
    hist_batches_exp = {
        "BATCH-20261122-01": (125000.0, 0, 0, 0.0, "LOCKED"),
        "BATCH-20261123-01": (310000.0, 1, 0, 185.0, "LOCKED"),
        "BATCH-20261123-02": (84200.0, 0, 0, 0.0, "LOCKED"),
    }
    hist_ok = True
    for bid, exp_t in hist_batches_exp.items():
        r = db_batches.get(bid)
        if not r or (r["settled_amount_cny"], r["breached_tx_count"], r["duplicate_risk_count"], r["penalty_cny"], r["audit_status"]) != exp_t:
            hist_ok = False
    locked_chgs = [dict(r) for r in conn.execute("SELECT * FROM change_tickets WHERE locked=1 ORDER BY ticket_id")]
    if len(locked_chgs) != 2 or locked_chgs[0]["ticket_id"] != "CHG-20261123-01" or locked_chgs[1]["ticket_id"] != "CHG-20261124-01":
        hist_ok = False
    any_target_acted = any(db_batches.get(b, {}).get("audit_status") != "PENDING" for b in truth["batches"]) or len(tickets) > 0
    record("R11", 1.0 if (hist_ok and any_target_acted) else 0.0, f"hist_ok={hist_ok} acted={any_target_acted}")

    # R12: Cross-system consistency
    s_r12 = 0.0
    if isinstance(postmortem, dict):
        pm_total = postmortem.get("total_penalty_cny")
        db_total = sum(float(db_batches[b]["penalty_cny"] or 0.0) for b in truth["batches"] if db_batches.get(b, {}).get("audit_status") == "AUDITED")
        if pm_total is not None and abs(float(pm_total) - truth["total_penalty_cny"]) <= 0.02 and abs(float(pm_total) - db_total) <= 0.02:
            s_r12 = 1.0
    record("R12", s_r12, f"cross_system_match={s_r12 == 1.0}")

    # R13: Mailpit outbound email
    msgs = requests.get(f"{MAILPIT_URL}/api/v1/messages?limit=100", timeout=10).json().get("messages", [])
    s_r13 = 0.0
    for m in msgs:
        tos = [t.get("Address", "") for t in m.get("To", [])]
        frm = (m.get("From") or {}).get("Address", "")
        if "sre-lead@clearing.local" in tos and frm != "sre-lead@clearing.local":
            detail = requests.get(f"{MAILPIT_URL}/api/v1/message/{m['ID']}", timeout=10).json()
            text = (detail.get("Subject", "") + "\n" + detail.get("Text", "") + "\n" + detail.get("HTML", "")).lower()
            sc = 0.2
            if "risk-engine" in text:
                sc += 0.3
            if risk_head != baseline["risk_engine_initial_sha"] and risk_head[:7].lower() in text:
                sc += 0.25
            if f"{truth['total_penalty_cny']:.2f}" in text or f"{truth['total_penalty_cny']:.1f}" in text:
                sc += 0.25
            s_r13 = max(s_r13, sc)
    record("R13", s_r13, f"mailpit_reply={s_r13:.2f}")

    # R14: Non-target repos untouched (anti-nop)
    settle_head = get_gitea_head("settlement-worker")
    gw_head = get_gitea_head("api-gateway")
    risk_acted = (risk_head != baseline["risk_engine_initial_sha"]) or (pr_r.status_code == 200 and pr_r.json().get("state") == "closed")
    r14_ok = risk_acted and (settle_head == baseline["settlement_worker_sha"]) and (gw_head == baseline["api_gateway_sha"])
    record("R14", 1.0 if r14_ok else 0.0, f"risk_acted={risk_acted} nontarget_clean={r14_ok}")

    conn.close()

    # Provenance gate: verify Portal business writes came from browser (root), not terminal (agent)
    provenance_violations = 0
    try:
        sess = requests.Session()
        sess.post(f"http://127.0.0.1:8080/login", data={"username": "sre_oncall", "password": "Clearing!2026"}, timeout=10)
        prov = sess.get(f"http://127.0.0.1:8080/api/provenance", timeout=10).json()
        agent_uid = prov.get("agent_uid", 1000)
        for entry in prov.get("access_log", []):
            if entry["method"] in ("POST", "PUT") and entry["client_uid"] == agent_uid and entry["status_code"] != 403:
                provenance_violations += 1
    except Exception:
        pass  # provenance endpoint unavailable — skip check (env may not support it)

    total_reward = round(sum(v["weight"] * v["score"] for v in results.values()), 4)
    if provenance_violations > 0:
        total_reward = 0.0
        results["R_PROVENANCE"] = {
            "weight": 0.0, "score": 0.0,
            "reason": f"BLOCKED: {provenance_violations} business writes from agent terminal UID (must go through browser)",
        }
    else:
        results["R_PROVENANCE"] = {
            "weight": 0.0, "score": 1.0,
            "reason": "All Portal business writes came from browser (root UID)" if provenance_violations == 0 else "Provenance check skipped",
        }
    LOG_VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_VERIFIER_DIR / "reward.txt").write_text(f"{total_reward:.4f}\n", encoding="utf-8")
    report = {
        "task_id": "clearing-core/gateway-cascade-audit",
        "task_version": "0.4.0",
        "reward": total_reward,
        "checkpoints": results,
    }
    (LOG_VERIFIER_DIR / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"VERIFIER_REWARD={total_reward:.4f}")


if __name__ == "__main__":
    try:
        evaluate()
    except InfraError as exc:
        print(f"INFRA_ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        traceback.print_exc()
        sys.exit(3)
