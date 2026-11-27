#!/usr/bin/env python3
"""Independent Python verifier for the 14 v0.5.0 task checkpoints + R_PROVENANCE."""
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
PORTAL_URL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8080")
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
    if not isinstance(win_str, str) or not re.search(r"2026-\d{2}-\d{2}", win_str):
        return None
    times = re.findall(r"(\d{1,2}):(\d{2})", win_str)
    if len(times) < 2:
        return None
    start_m = int(times[0][0]) * 60 + int(times[0][1])
    end_m = int(times[1][0]) * 60 + int(times[1][1])
    if end_m <= start_m:
        return None
    return start_m, end_m


def score_cmdb(conn: sqlite3.Connection, baseline_path: pathlib.Path, truth: dict) -> tuple[float, str, bool]:
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
        node for node in protected if current.get(node, {}).get("status") != baseline[node]["status"]
    } | (protected & latest.keys())
    unexpected_nodes = current.keys() - baseline.keys()
    protected_ok = not protected_changed and not unexpected_nodes
    score = len(isolated) / len(targets)
    if not protected_ok:
        score *= 0.5
    return score, (
        f"isolated={len(isolated)}/{len(targets)} "
        f"protected_changed={sorted(protected_changed)} unexpected_nodes={sorted(unexpected_nodes)}"
    ), protected_ok


def evaluate() -> None:
    if not TRUTH_PATH.exists() or not BASELINE_PATH.exists() or not PORTAL_DB.exists():
        raise InfraError("Required verifier truth, baseline, or portal DB missing")

    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    postmortem = load_json_safe(OUTPUT_DIR / "incident_postmortem.json")

    weights = {
        "R01": 0.04,
        "R02": 0.08,
        "R03": 0.06,
        "R04": 0.06,
        "R05": 0.12,
        "R06": 0.06,
        "R07": 0.08,
        "R08": 0.07,
        "R09": 0.10,
        "R10": 0.08,
        "R11": 0.10,
        "R12": 0.05,
        "R13": 0.06,
        "R14": 0.04,
    }
    assert abs(sum(weights.values()) - 1.0) < 1e-9

    results = {}

    def record(cid: str, score: float, reason: str, evidence: str = ""):
        results[cid] = {
            "weight": weights[cid],
            "score": round(max(0.0, min(1.0, float(score))), 4),
            "reason": reason,
            "evidence": evidence,
        }

    # R01: Basic fault attribution (service + version + Perfetto bottleneck span)
    s_r01 = 0.0
    if isinstance(postmortem, dict):
        if str(postmortem.get("root_cause_service", "")).strip() == "risk-engine":
            s_r01 += 0.34
        if "2.8.4" in str(postmortem.get("culprit_version", "")):
            s_r01 += 0.33
        span_val = str(postmortem.get("bottleneck_span", "")).lower()
        if any(k in span_val for k in ("acquire_rule_lock", "fx_quote_http_retry", "_rule_cache_lock", "refresh_fx_quote")):
            s_r01 += 0.33
    record("R01", s_r01, f"basic_attribution={s_r01:.2f}")

    # R02: High-res telemetry drill-down attribution (first_onset_node, exact 5s onset, transient_recovered_node)
    s_r02 = 0.0
    if isinstance(postmortem, dict):
        if str(postmortem.get("first_onset_node", "")).strip() == truth["first_onset_node"]:
            s_r02 += 0.35
        d_onset = parse_dt_utc8(str(postmortem.get("onset_time_utc8", "")))
        t_low = dt.datetime(2026, 11, 24, 14, 13, 0, tzinfo=TZ_UTC8)
        t_high = dt.datetime(2026, 11, 24, 14, 14, 10, tzinfo=TZ_UTC8)
        if d_onset and t_low <= d_onset <= t_high:
            s_r02 += 0.30
        if str(postmortem.get("transient_recovered_node", "")).strip() == truth["transient_recovered_node"]:
            s_r02 += 0.35
    record("R02", s_r02, f"drilldown_score={s_r02:.2f}")

    # R03: Realistic trap PR #1 closed & not merged with justification comment
    pr_r = requests.get(f"{GITEA_URL}/api/v1/repos/clearing-core/risk-engine/pulls/1", auth=(GITEA_USER, GITEA_PASS), timeout=10)
    if pr_r.status_code == 200:
        pr = pr_r.json()
        cmts_r = requests.get(f"{GITEA_URL}/api/v1/repos/clearing-core/risk-engine/issues/1/comments", auth=(GITEA_USER, GITEA_PASS), timeout=10)
        cmts = " ".join(c.get("body", "") for c in (cmts_r.json() if cmts_r.status_code == 200 else [])).lower()
        s_r03 = 0.0
        if pr.get("state") == "closed" and not pr.get("merged"):
            s_r03 += 0.5
        if any(k in cmts for k in ("180", "220", "max_connections", "pool", "single-flight", "singleflight", "连接")):
            s_r03 += 0.5
        record("R03", s_r03, f"PR#1 state={pr.get('state')} merged={pr.get('merged')}")
    else:
        record("R03", 0.0, f"Failed fetching PR#1: {pr_r.status_code}")

    # R04: Gitea risk-engine main HEAD changed & DB_POOL_SIZE <= 180 (gated by non-target repo protection)
    risk_head = get_gitea_head("risk-engine")
    settle_head = get_gitea_head("settlement-worker")
    gw_head = get_gitea_head("api-gateway")
    nontarget_clean = (settle_head == baseline["settlement_worker_sha"]) and (gw_head == baseline["api_gateway_sha"])

    cfg_r = requests.get(f"{GITEA_URL}/api/v1/repos/clearing-core/risk-engine/raw/main/risk_engine/config.py", auth=(GITEA_USER, GITEA_PASS), timeout=10)
    pool_ok = False
    if cfg_r.status_code == 200:
        m = re.search(r"DB_POOL_SIZE\s*=\s*(\d+)", cfg_r.text)
        if m and int(m.group(1)) <= 180:
            pool_ok = True
    s_r04 = 0.0
    if risk_head != baseline["risk_engine_initial_sha"] and pool_ok:
        s_r04 = 1.0
    elif risk_head != baseline["risk_engine_initial_sha"]:
        s_r04 = 0.4
    if not nontarget_clean:
        s_r04 = 0.0
    record("R04", s_r04, f"main={risk_head[:10]} pool_ok={pool_ok} nontarget_clean={nontarget_clean}")

    # R05: 5-part independent SWE regression suite on cloned risk-engine main HEAD
    if risk_head == baseline["risk_engine_initial_sha"]:
        record("R05", 0.0, "Skipped on unmodified buggy v2.8.4")
    else:
        with tempfile.TemporaryDirectory() as td:
            clone_url = GITEA_URL.replace("://", f"://{GITEA_USER}:{GITEA_PASS}@") + "/clearing-core/risk-engine.git"
            cp = subprocess.run(["git", "clone", "--depth", "5", clone_url, td], capture_output=True, text=True)
            if cp.returncode != 0:
                record("R05", 0.0, f"git clone failed: {cp.stderr}")
            else:
                test_script = r'''
import concurrent.futures, sys, time
sys.path.insert(0, ".")
from risk_engine.engine import RiskEngine
from risk_engine.fx_client import FxClient

passed = 0

# 1) Cross-pair non-blocking
def slow_fetch(pair):
    if pair == "USD_CNY":
        time.sleep(0.40)
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

# 2) Same-pair Single-Flight (6 concurrent expired USD_CNY calls -> only 1 upstream call, no 429)
sf_client = FxClient(lambda p: (time.sleep(0.25) or 7.18), max_concurrent_per_pair=1)
eng_sf = RiskEngine(fx_client=sf_client, cache_ttl=30.0)
eng_sf._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 100.0)
sf_ok = True
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    futs = [ex.submit(eng_sf.evaluate_tx, f"TX-SF-{i}", "M1", "USD", 10.0, "2026-11-24T14:15:01+08:00") for i in range(6)]
    for f in futs:
        try:
            r = f.result(timeout=5)
            if r["amount_cny"] != 71.8:
                sf_ok = False
        except Exception:
            sf_ok = False
if sf_ok and sf_client.call_counts.get("USD_CNY", 0) == 1:
    passed += 1

# 3) 503 stale cache fallback on known pair + error raised on unknown uncached pair
eng2 = RiskEngine(fx_client=FxClient(lambda p: (_ for _ in ()).throw(RuntimeError("503"))), cache_ttl=1.0)
eng2._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 10.0)
stale_ok = False
try:
    r_fb = eng2.evaluate_tx("TX-FB", "M1", "USD", 10.0, "2026-11-24T14:15:01+08:00")
    stale_ok = (r_fb["amount_cny"] == 71.5)
except Exception:
    stale_ok = False
unk_raised = False
try:
    eng2.evaluate_tx("TX-UNK", "M1", "EUR", 10.0, "2026-11-24T14:15:01+08:00")
except Exception:
    unk_raised = True
if stale_ok and unk_raised:
    passed += 1

# 4) Idempotency uniqueness & cross-retry / cross-TZ invariance
k1 = eng.generate_idempotency_key("TX-1001", "MCH_VIP_001", "2026-11-24T14:15:01+08:00")
k2 = eng.generate_idempotency_key("TX-1002", "MCH_VIP_001", "2026-11-24T14:15:45+08:00")
k1_retry = eng.generate_idempotency_key("TX-1001", "MCH_VIP_001", "2026-11-24T14:15:45+08:00")
k1_utc = eng.generate_idempotency_key("TX-1001", "MCH_VIP_001", "2026-11-24T06:15:01Z")
if k1 != k2 and k1 == k1_retry and k1 == k1_utc:
    passed += 1

print(f"REGRESSION_RESULT={passed}/4")
'''
                rp = subprocess.run([sys.executable, "-c", test_script], cwd=td, capture_output=True, text=True, timeout=20)
                pt = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=td, capture_output=True, text=True, timeout=20)
                m_reg = re.search(r"REGRESSION_RESULT=(\d+)/4", rp.stdout)
                reg_n = int(m_reg.group(1)) if m_reg else 0
                pytest_ok = 1 if pt.returncode == 0 else 0
                record("R05", (reg_n + pytest_ok) / 5.0, f"reg={reg_n}/4 pytest_ok={pytest_ok}")

    conn = sqlite3.connect(str(PORTAL_DB))
    conn.row_factory = sqlite3.Row

    # R06: 10-node CMDB isolation with protected-node status history check
    s_r06, r06_reason, protected_ok = score_cmdb(conn, CMDB_BASELINE_DB, truth)
    record("R06", s_r06, r06_reason, evidence=str(PORTAL_DB))

    # R07: CMDB traffic weight rebalancing (w2=0, w3=0, active sum=100, each <= cap and canary<=30%)
    db_nodes = {r["node_name"]: dict(r) for r in conn.execute("SELECT * FROM cmdb_nodes")}
    constraints = truth["rebalance_constraints"]
    active_caps = constraints["active_risk_nodes"]
    iso_zeros = constraints["isolated_zero_nodes"]
    w_iso_ok = all(int(db_nodes.get(n, {}).get("traffic_weight", -1)) == 0 for n in iso_zeros)
    w_active = {n: int(db_nodes.get(n, {}).get("traffic_weight", -1)) for n in active_caps}
    sum_ok = (sum(w_active.values()) == int(constraints["total_weight"]))
    caps_ok = all(0 <= w_active[n] <= cap for n, cap in active_caps.items())
    # Must actually rebalance (at least one active weight changed from initial 10/20/10)
    rebalanced_moved = (w_active != {"risk-node-01": 10, "risk-node-04": 20, "risk-node-05": 10})
    s_r07 = 0.0
    if s_r06 > 0 and w_iso_ok:
        s_r07 += 0.3
    if sum_ok and rebalanced_moved:
        s_r07 += 0.35
    if caps_ok and rebalanced_moved:
        s_r07 += 0.35
    if not protected_ok:
        s_r07 *= 0.5
    record("R07", s_r07, f"weights={w_active} iso_zero={w_iso_ok} sum_ok={sum_ok} caps_ok={caps_ok}")

    # R08: Canary Replay passed on new main HEAD with valid dynamic fingerprint
    tickets = [dict(r) for r in conn.execute("SELECT * FROM change_tickets WHERE locked=0 ORDER BY ticket_id")]
    best_r08 = 0.0
    matched_ticket = None
    for t in tickets:
        if len(t["commit_sha"]) >= 7 and risk_head.startswith(t["commit_sha"]) and risk_head != baseline["risk_engine_initial_sha"]:
            sc = 0.0
            if t.get("canary_status") == "CANARY_PASSED":
                sc += 0.7
            if str(t.get("canary_fingerprint") or "").startswith("CFP-"):
                sc += 0.3
            if sc > best_r08:
                best_r08 = sc
                matched_ticket = t
    record("R08", best_r08, f"canary_score={best_r08:.2f} fp={(matched_ticket or {}).get('canary_fingerprint','')}")

    # R09: CAB dynamic second-round approval (revision_count >= 1, APPROVED, fingerprint in desc, window avoids 17:30-20:00)
    best_r09 = 0.0
    r09_msg = "No revised APPROVED ticket"
    for t in tickets:
        if not (len(t["commit_sha"]) >= 7 and risk_head.startswith(t["commit_sha"]) and risk_head != baseline["risk_engine_initial_sha"]):
            continue
        sc = 0.0
        fp = str(t.get("canary_fingerprint") or "").strip()
        if int(t.get("revision_count") or 0) >= 1:
            sc += 0.2
        if t.get("status") == "APPROVED":
            sc += 0.3
        if fp and fp.lower() in str(t.get("description") or "").lower():
            sc += 0.25
        win = parse_window_hhmm(t.get("scheduled_window_utc8", ""))
        if win:
            s_m, e_m = win
            if e_m <= 1050 or s_m >= 1200:
                sc += 0.25
        if sc > best_r09:
            best_r09 = sc
            r09_msg = f"ticket={t['ticket_id']} status={t.get('status')} rev={t.get('revision_count')} win={t.get('scheduled_window_utc8')}"
    record("R09", best_r09, r09_msg)

    # R10: Local CSV + duplicate_risk_tx_ids + dynamic canary_run_fingerprint binding
    csv_path = OUTPUT_DIR / "reconciliation_summary.csv"
    s_r10 = 0.0
    csv_rows = []
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8") as f:
            csv_rows = list(csv.DictReader(f))
        bids = {r.get("batch_id") for r in csv_rows}
        has_test = any(str(r.get("merchant_id", "")).startswith("TEST_") for r in csv_rows)
        if set(truth["batches"].keys()).issubset(bids) and not has_test:
            s_r10 += 0.35
    if isinstance(postmortem, dict):
        if set(postmortem.get("duplicate_risk_tx_ids") or []) == set(truth["duplicate_risk_tx_ids"]):
            s_r10 += 0.35
        expected_fp = str((matched_ticket or {}).get("canary_fingerprint") or "").strip()
        got_fp = str(postmortem.get("canary_run_fingerprint") or "").strip()
        if expected_fp and got_fp == expected_fp:
            s_r10 += 0.30
    record("R10", s_r10, f"local_clean_fp_score={s_r10:.2f}")

    # R11: Portal DB SLA batches matching Mutex Highest-Only + Clause 4.2 + REV-2 (gated by locked=1 history protection)
    db_batches = {r["batch_id"]: dict(r) for r in conn.execute("SELECT * FROM sla_batches")}
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
    s_r11 = (matched_b / len(truth["batches"])) if hist_ok else 0.0
    record("R11", s_r11, f"matched_batches={int(matched_b)}/{len(truth['batches'])} hist_ok={hist_ok}")

    # R12: Cross-system numerical consistency (JSON vs Portal DB vs CSV vs truth)
    s_r12 = 0.0
    if isinstance(postmortem, dict):
        pm_total = postmortem.get("total_penalty_cny")
        db_total = sum(float(db_batches[b]["penalty_cny"] or 0.0) for b in truth["batches"] if db_batches.get(b, {}).get("audit_status") == "AUDITED")
        csv_map = {r.get("batch_id"): r for r in csv_rows}
        csv_db_match = bool(csv_map) and all(
            bid in csv_map
            and db_batches.get(bid, {}).get("audit_status") == "AUDITED"
            and abs(float(csv_map[bid]["settled_amount_cny"]) - float(db_batches[bid]["settled_amount_cny"])) <= 0.05
            and abs(float(csv_map[bid]["penalty_cny"]) - float(db_batches[bid]["penalty_cny"])) <= 0.02
            for bid in truth["batches"]
        )
        if pm_total is not None and abs(float(pm_total) - truth["total_penalty_cny"]) <= 0.02 and abs(float(pm_total) - db_total) <= 0.02:
            s_r12 += 0.5
        if csv_db_match:
            s_r12 += 0.5
    record("R12", s_r12, f"cross_system_score={s_r12:.2f}")

    # R13: Postmortem JSON completeness (traffic_weights match DB, batches match DB, extended_freeze_window_utc8)
    s_r13 = 0.0
    if isinstance(postmortem, dict):
        pm_w = postmortem.get("traffic_weights")
        if isinstance(pm_w, dict) and all(int(pm_w.get(n, -1)) == int(db_nodes[n]["traffic_weight"]) for n in ["risk-node-01", "risk-node-02", "risk-node-03", "risk-node-04", "risk-node-05"]):
            s_r13 += 0.35
        ext_fw = str(postmortem.get("extended_freeze_window_utc8") or "")
        if "17:30" in ext_fw and "20:00" in ext_fw:
            s_r13 += 0.35
        pm_batches = postmortem.get("batches")
        if isinstance(pm_batches, dict) and set(truth["batches"].keys()).issubset(pm_batches.keys()):
            if all(abs(float(pm_batches[b].get("penalty_cny", -1)) - truth["batches"][b]["penalty_cny"]) <= 0.02 for b in truth["batches"]):
                s_r13 += 0.30
    record("R13", s_r13, f"postmortem_completeness={s_r13:.2f}")

    # R14: Mailpit closing email with risk-engine, SHA prefix, total penalty, AND dynamic canary_run_fingerprint
    msgs = requests.get(f"{MAILPIT_URL}/api/v1/messages?limit=100", timeout=10).json().get("messages", [])
    s_r14 = 0.0
    exp_fp = str((matched_ticket or {}).get("canary_fingerprint") or "").strip().lower()
    for m in msgs:
        tos = [t.get("Address", "") for t in m.get("To", [])]
        frm = (m.get("From") or {}).get("Address", "")
        if "sre-lead@clearing.local" in tos and frm != "sre-lead@clearing.local":
            detail = requests.get(f"{MAILPIT_URL}/api/v1/message/{m['ID']}", timeout=10).json()
            text = (detail.get("Subject", "") + "\n" + detail.get("Text", "") + "\n" + detail.get("HTML", "")).lower()
            sc = 0.0
            if "risk-engine" in text:
                sc += 0.25
            if risk_head != baseline["risk_engine_initial_sha"] and risk_head[:7].lower() in text:
                sc += 0.25
            if f"{truth['total_penalty_cny']:.2f}" in text or f"{truth['total_penalty_cny']:.1f}" in text:
                sc += 0.25
            if exp_fp and exp_fp in text:
                sc += 0.25
            s_r14 = max(s_r14, sc)
    record("R14", s_r14, f"mailpit_reply={s_r14:.2f}")

    conn.close()

    # Provenance gate
    provenance_violations = 0
    provenance_ok = False
    try:
        sess = requests.Session()
        sess.post(f"{PORTAL_URL}/login", data={"username": "sre_oncall", "password": "Clearing!2026"}, timeout=10)
        prov = sess.get(f"{PORTAL_URL}/api/provenance", timeout=10).json()
        agent_uid = prov.get("agent_uid", 1000)
        for entry in prov.get("access_log", []):
            if entry["method"] in ("POST", "PUT") and entry["client_uid"] == agent_uid and entry["status_code"] != 403:
                provenance_violations += 1
        provenance_ok = True
    except Exception as e:
        results["R_PROVENANCE"] = {
            "weight": 0.0, "score": 0.0,
            "reason": f"BLOCKED: provenance check failed: {e}",
        }

    total_reward = round(sum(v["weight"] * v["score"] for v in results.values()), 4)
    if provenance_violations > 0 or not provenance_ok:
        total_reward = 0.0
    if "R_PROVENANCE" not in results:
        results["R_PROVENANCE"] = {
            "weight": 0.0, "score": 1.0 if provenance_violations == 0 else 0.0,
            "reason": f"violations={provenance_violations}" if provenance_ok else "provenance check failed",
        }
    LOG_VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_VERIFIER_DIR / "reward.txt").write_text(f"{total_reward:.4f}\n", encoding="utf-8")
    report = {
        "task_id": "clearing-core/gateway-cascade-audit",
        "task_version": "0.5.0",
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
