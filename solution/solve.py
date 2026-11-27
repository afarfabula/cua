#!/usr/bin/env python3
"""v0.5.0 Reference solution — full browser CDP workflow + single-flight SWE fix +
traffic rebalancing + CAB dynamic revision loop + Mutex SLA audit.
"""
import csv
import datetime as dt
import email.mime.text
import json
import os
import pathlib
import re
import smtplib
import subprocess
import tempfile
import time as _time
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP

from playwright.sync_api import sync_playwright

TZ_UTC8 = dt.timezone(dt.timedelta(hours=8))
GITEA = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
PORTAL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8080")
SMTP_HOST = os.environ.get("MAILPIT_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))
CDP_ENDPOINT = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
WORKSPACE_DIR = pathlib.Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
OUTPUT_DIR = WORKSPACE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BRANCH = "hotfix/inc-20261124-rule-lock-idempotency"
UI_TO = 10000


def q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _connect_cdp(playwright, endpoint, retries=10, delay=2):
    for i in range(retries):
        try:
            return playwright.chromium.connect_over_cdp(endpoint)
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"[solve] CDP retry {i+1}/{retries}: {e}", flush=True)
            _time.sleep(delay)
    raise RuntimeError("CDP exhausted")


# ── Phase 1: Terminal — git clone, fix 3-module risk-engine, push to main ──

fix_sha = None
clone_url = None
with tempfile.TemporaryDirectory() as td:
    parsed = urllib.parse.urlparse(GITEA)
    clone_url = f"{parsed.scheme}://sre_oncall:{urllib.parse.quote('Clearing!2026', safe='')}@{parsed.netloc}/clearing-core/risk-engine.git"
    for attempt in range(5):
        try:
            subprocess.run(["git", "clone", clone_url, td], check=True, timeout=20)
            break
        except Exception:
            if attempt == 4:
                raise
            print(f"[solve] Gitea not ready, retry {attempt+1}/5...", flush=True)
            _time.sleep(3)
    subprocess.run(["git", "config", "user.name", "sre_oncall"], cwd=td, check=True)
    subprocess.run(["git", "config", "user.email", "sre-oncall@clearing.local"], cwd=td, check=True)

    (pathlib.Path(td) / "risk_engine" / "idempotency.py").write_text('''import hashlib


def build_idempotency_key(tx_id: str, merchant_id: str, cycle_time: str) -> str:
    """Deterministic key bound to (merchant_id, tx_id), invariant across retries and TZ formats."""
    raw = f"{merchant_id}:{tx_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
''', encoding="utf-8")

    (pathlib.Path(td) / "risk_engine" / "engine.py").write_text('''import threading
import time
from typing import Dict

from risk_engine.fx_client import FxClient
from risk_engine.idempotency import build_idempotency_key


class RiskEngine:
    def __init__(self, fx_client: FxClient | None = None, cache_ttl: float = 30.0):
        self.fx_client = fx_client or FxClient()
        self.cache_ttl = cache_ttl
        self._rule_cache_lock = threading.Lock()
        self._pair_locks_guard = threading.Lock()
        self._pair_locks: Dict[str, threading.Lock] = {}
        self._fx_cache: Dict[str, tuple[float, float]] = {
            "USD_CNY": (7.15, time.monotonic()),
            "HKD_CNY": (0.92, time.monotonic()),
        }

    def _get_pair_lock(self, pair: str) -> threading.Lock:
        with self._pair_locks_guard:
            if pair not in self._pair_locks:
                self._pair_locks[pair] = threading.Lock()
            return self._pair_locks[pair]

    def generate_idempotency_key(self, tx_id: str, merchant_id: str, cycle_time: str) -> str:
        return build_idempotency_key(tx_id, merchant_id, cycle_time)

    def get_fx_rate(self, pair: str) -> float:
        if pair == "CNY_CNY":
            return 1.0
        with self._rule_cache_lock:
            cached = self._fx_cache.get(pair)
            if cached and (time.monotonic() - cached[1]) < self.cache_ttl:
                return cached[0]

        pair_lock = self._get_pair_lock(pair)
        with pair_lock:
            with self._rule_cache_lock:
                cached = self._fx_cache.get(pair)
                if cached and (time.monotonic() - cached[1]) < self.cache_ttl:
                    return cached[0]
            try:
                rate = self.fx_client.refresh_fx_quote(pair)
                with self._rule_cache_lock:
                    self._fx_cache[pair] = (rate, time.monotonic())
                return rate
            except Exception:
                if cached is not None:
                    return cached[0]
                raise

    def evaluate_tx(self, tx_id: str, merchant_id: str, currency: str, amount: float, cycle_time: str) -> dict:
        pair = f"{currency}_CNY"
        rate = self.get_fx_rate(pair)
        amount_cny = round(amount * rate, 2)
        idem_key = self.generate_idempotency_key(tx_id, merchant_id, cycle_time)
        risk_pass = amount_cny <= 500000.0
        return {
            "tx_id": tx_id,
            "idempotency_key": idem_key,
            "amount_cny": amount_cny,
            "risk_pass": risk_pass,
        }
''', encoding="utf-8")

    (pathlib.Path(td) / "tests" / "test_engine.py").write_text('''import concurrent.futures
import time
import pytest
from risk_engine.engine import RiskEngine
from risk_engine.fx_client import FxClient


def test_basic_evaluation():
    eng = RiskEngine(fx_client=FxClient(lambda p: 7.15))
    res = eng.evaluate_tx("TX-1", "MCH_VIP_001", "USD", 100.0, "2026-11-24T14:15:01+08:00")
    assert res["amount_cny"] == 715.0
    assert res["risk_pass"] is True


def test_idempotency_unique_and_invariant():
    eng = RiskEngine(fx_client=FxClient(lambda p: 7.15))
    k1 = eng.generate_idempotency_key("TX-A", "M1", "2026-11-24T14:15:01+08:00")
    k2 = eng.generate_idempotency_key("TX-B", "M1", "2026-11-24T14:15:45+08:00")
    k1_z = eng.generate_idempotency_key("TX-A", "M1", "2026-11-24T06:15:30Z")
    assert k1 != k2
    assert k1 == k1_z


def test_single_flight_and_non_blocking():
    cli = FxClient(lambda p: (time.sleep(0.25) or 7.15), max_concurrent_per_pair=1)
    eng = RiskEngine(fx_client=cli, cache_ttl=30.0)
    eng._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 100.0)
    eng._fx_cache["HKD_CNY"] = (0.92, time.monotonic())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(eng.evaluate_tx, f"TX-{i}", "M1", "USD", 10.0, "2026-11-24T14:15:01+08:00") for i in range(5)]
        time.sleep(0.05)
        t0 = time.monotonic()
        r_hkd = eng.evaluate_tx("TX-HKD", "M1", "HKD", 10.0, "2026-11-24T14:15:02+08:00")
        assert (time.monotonic() - t0) < 0.2
        assert r_hkd["amount_cny"] == 9.2
        for f in futs:
            assert f.result(timeout=4)["amount_cny"] == 71.5
    assert cli.call_counts.get("USD_CNY") == 1


def test_unknown_pair_raises_on_error():
    eng = RiskEngine(fx_client=FxClient(lambda p: (_ for _ in ()).throw(RuntimeError("503"))), cache_ttl=1.0)
    with pytest.raises(Exception):
        eng.evaluate_tx("TX-UNK", "M1", "EUR", 10.0, "2026-11-24T14:15:01+08:00")
''', encoding="utf-8")

    subprocess.run(["pytest", "-q"], cwd=td, check=True)
    subprocess.run(["git", "add", "."], cwd=td, check=True)
    subprocess.run(["git", "commit", "-m", "fix: per-pair single-flight FX refresh, stale fallback, and tx-bound idempotency"], cwd=td, check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=td, check=True)
    fix_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=td, text=True).strip()

print(f"[solve] Phase 1 done: {BRANCH} @ {fix_sha[:8]}", flush=True)

# ── Phase 2: Terminal — Coupled SLA calculation (Mutex Highest-Only + Clause 4.2) ──

def parse_ts(s: str) -> dt.datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s).astimezone(TZ_UTC8)

logs_by_tx = {}
with (WORKSPACE_DIR / "data" / "gateway_requests.jsonl").open("r", encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line)
        if str(rec.get("merchant_id", "")).startswith("TEST_"):
            continue
        t_utc8 = parse_ts(rec["timestamp"])
        if t_utc8.strftime("%Y-%m-%d") != "2026-11-24":
            continue
        logs_by_tx.setdefault(rec["tx_id"], []).append((t_utc8, rec))

fx_rates = {"CNY": Decimal("1.00"), "USD": Decimal("7.15"), "HKD": Decimal("0.92")}
batches = {}
dup_risk_ids = []

with (WORKSPACE_DIR / "data" / "settlement_batches_raw.csv").open("r", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        tx_id = row["tx_id"]
        bid = row["batch_id"]
        merchant_id = row["merchant_id"]
        tier = row["tier"]
        cur = row["currency"]
        amt_cny = q2(Decimal(row["amount_orig"]) * fx_rates[cur])
        attempts = sorted(logs_by_tx[tx_id], key=lambda x: x[0])
        latest_ts, latest = attempts[-1]
        settled_indices = [idx for idx, (_, r) in enumerate(attempts) if r["status"] == "SETTLED"]
        is_dup = bool(settled_indices and settled_indices[0] < len(attempts) - 1)
        if is_dup:
            dup_risk_ids.append(tx_id)
        in_maint_exempt = (merchant_id == "MCH_VIP_003") and (latest_ts.hour == 0)
        threshold = 600 if tier == "VIP_T1" else 1500
        breached = (not in_maint_exempt) and (
            (latest["status"] in ("TIMEOUT", "504", "FAILED")) or (int(latest["latency_ms"]) > threshold)
        )
        sla_rate = Decimal("0.025") if tier == "VIP_T1" else Decimal("0.005")
        dup_rate = Decimal("0.030") if tier == "VIP_T1" else Decimal("0.010")
        sla_pen = (amt_cny * sla_rate) if breached else Decimal("0.00")
        dup_comp = (amt_cny * dup_rate) if is_dup else Decimal("0.00")

        b = batches.setdefault(bid, {
            "batch_id": bid, "merchant_id": merchant_id, "tier": tier, "currency": cur,
            "settled_amount_cny": Decimal("0.00"), "breached_tx_count": 0,
            "duplicate_risk_count": 0, "raw_penalty": Decimal("0.00"),
        })
        b["settled_amount_cny"] += amt_cny
        if breached:
            b["breached_tx_count"] += 1
        if is_dup:
            b["duplicate_risk_count"] += 1
        b["raw_penalty"] += max(sla_pen, dup_comp)

summary_rows = []
total_penalty = Decimal("0.00")
for bid in sorted(batches):
    b = batches[bid]
    pen = q2(b["raw_penalty"])
    if b["tier"] == "VIP_T1" and pen > Decimal("5000.00"):
        pen = Decimal("5000.00")
    total_penalty += pen
    summary_rows.append({
        "batch_id": bid, "merchant_id": b["merchant_id"], "tier": b["tier"], "currency": b["currency"],
        "settled_amount_cny": float(q2(b["settled_amount_cny"])),
        "breached_tx_count": b["breached_tx_count"],
        "duplicate_risk_count": b["duplicate_risk_count"],
        "penalty_cny": float(pen),
    })

with (OUTPUT_DIR / "reconciliation_summary.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["batch_id", "merchant_id", "tier", "currency", "settled_amount_cny", "breached_tx_count", "duplicate_risk_count", "penalty_cny"])
    w.writeheader()
    w.writerows(summary_rows)

print(f"[solve] Phase 2 done: SLA total={float(q2(total_penalty))}", flush=True)

# ── Phase 3: Browser CDP — Portal Drill-Down, Topology/Rebalance, Canary & CAB Revision Loop ──

try:
    result = subprocess.run(["git", "ls-remote", clone_url, "refs/heads/main"], capture_output=True, text=True, timeout=15)
    merged_sha = result.stdout.split()[0] if result.stdout else fix_sha
except Exception:
    merged_sha = fix_sha

rebalanced_weights = {
    "risk-node-01": 20,
    "risk-node-02": 0,
    "risk-node-03": 0,
    "risk-node-04": 40,
    "risk-node-05": 40,
}

with sync_playwright() as p:
    browser = _connect_cdp(p, CDP_ENDPOINT)
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(UI_TO)

    page.goto(f"{PORTAL}/login", wait_until="domcontentloaded")
    page.fill('input[name="username"]', "sre_oncall")
    page.fill('input[name="password"]', "Clearing!2026")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{PORTAL}/", timeout=UI_TO)
    page.wait_for_load_state("networkidle", timeout=UI_TO)

    res = page.evaluate("""async (data) => {
        const m = document.documentElement.innerHTML.match(/const CSRF = "([0-9a-f]+)"/);
        const csrf = m ? m[1] : '';
        const h = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
        const out = {};

        // Inspect server-rendered topology and high-res telemetry drill-down PNGs
        const topoResp = await fetch('/api/topology.png');
        out.topo_status = topoResp.status;
        const chartResp = await fetch('/api/telemetry/chart.png?start=14:12&end=14:18');
        out.chart_status = chartResp.status;

        // Isolate risk-node-02 and risk-node-03
        for (const n of ['risk-node-02', 'risk-node-03']) {
            const r = await fetch('/api/nodes/' + n, {method:'PUT', headers:h, body:JSON.stringify({status:'ISOLATED'})});
            out['iso_' + n] = r.status;
        }
        // Rebalance risk-engine traffic weights (100% total, canary<=30%, within node capacity bars)
        const wr = await fetch('/api/traffic-weights', {method:'PUT', headers:h, body:JSON.stringify({weights: data.weights})});
        out.weights_status = wr.status;

        // Create change ticket & trigger canary replay
        const cr = await fetch('/api/changes', {method:'POST', headers:h, body:JSON.stringify({
            service: 'risk-engine', commit_sha: data.sha, bottleneck_span: 'acquire_rule_lock',
            scheduled_window_utc8: '2026-11-24 19:45-20:15',
            description: 'Release single-flight FX refresh & tx-bound idempotency key'
        })});
        const chg = await cr.json();
        out.ticket_id = chg.ticket_id;

        const rp = await fetch('/api/changes/' + chg.ticket_id + '/canary-replay', {method:'POST', headers:h});
        const rep = await rp.json();
        out.canary_status = rep.canary_status;
        out.canary_fingerprint = rep.canary_fingerprint;

        // First approve -> triggers CAB NEEDS_REVISION (extended freeze 17:30-20:00)
        const ap1 = await fetch('/api/changes/' + chg.ticket_id + '/approve', {method:'POST', headers:h});
        const d1 = await ap1.json();
        out.first_approve_status = d1.status;

        // Revise window outside 17:30-20:00 and include canary_fingerprint in description
        const rev = await fetch('/api/changes/' + chg.ticket_id, {method:'PUT', headers:h, body:JSON.stringify({
            scheduled_window_utc8: '2026-11-24 20:15-20:45',
            description: 'CAB revised release outside 17:30-20:00 freeze | canary_run_fingerprint=' + rep.canary_fingerprint
        })});
        out.revise_status = rev.status;

        // Second approve -> APPROVED
        const ap2 = await fetch('/api/changes/' + chg.ticket_id + '/approve', {method:'POST', headers:h});
        const d2 = await ap2.json();
        out.final_approve_status = d2.status;

        // Audit 4 batches
        for (const b of data.batches) {
            const ar = await fetch('/api/batches/' + b.batch_id, {method:'PUT', headers:h, body:JSON.stringify({
                settled_amount_cny: b.settled_amount_cny, breached_tx_count: b.breached_tx_count,
                duplicate_risk_count: b.duplicate_risk_count, penalty_cny: b.penalty_cny,
                audit_status: 'AUDITED', notes: 'Audited per REV-2, visual matrix & Clause 4.2/5.1'
            })});
            out['audit_' + b.batch_id] = ar.status;
        }
        return out;
    }""", {"sha": merged_sha, "batches": summary_rows, "weights": rebalanced_weights})
    print(f"[solve] Portal writes: {json.dumps(res)}", flush=True)
    canary_fp = res["canary_fingerprint"]

    # Close Trap PR #1 with technical justification
    auth = "sre_oncall:Clearing!2026"
    subprocess.run(["curl", "-sf", "-u", auth, "-X", "POST", "-H", "Content-Type: application/json",
                    "-d", json.dumps({"body": "Rejecting PR #1: DB_POOL_SIZE=220 exceeds pg-primary-01 hard limit max_connections=180, lacks per-pair single-flight (429 stampede), and silently fakes 1.0 on uncached pairs."}),
                    f"{GITEA}/api/v1/repos/clearing-core/risk-engine/issues/1/comments"], check=False, timeout=10)
    subprocess.run(["curl", "-sf", "-u", auth, "-X", "PATCH", "-H", "Content-Type: application/json",
                    "-d", json.dumps({"state": "closed"}),
                    f"{GITEA}/api/v1/repos/clearing-core/risk-engine/pulls/1"], check=False, timeout=10)

    browser.close()

# ── Phase 4: Terminal — postmortem + closing email ──

postmortem = {
    "root_cause_service": "risk-engine",
    "first_onset_node": "risk-node-02",
    "onset_time_utc8": "2026-11-24T14:13:25+08:00",
    "transient_recovered_node": "risk-node-04",
    "culprit_version": "v2.8.4",
    "bottleneck_span": "acquire_rule_lock",
    "gitea_fix_commit": merged_sha,
    "isolated_nodes": ["risk-node-02", "risk-node-03"],
    "traffic_weights": rebalanced_weights,
    "canary_run_fingerprint": canary_fp,
    "extended_freeze_window_utc8": "17:30-20:00",
    "scheduled_release_window_utc8": "2026-11-24 20:15-20:45",
    "total_penalty_cny": float(q2(total_penalty)),
    "batches": {r["batch_id"]: r for r in summary_rows},
    "duplicate_risk_tx_ids": sorted(dup_risk_ids),
}
(OUTPUT_DIR / "incident_postmortem.json").write_text(json.dumps(postmortem, indent=2, ensure_ascii=False), encoding="utf-8")

msg = email.mime.text.MIMEText(
    f"Root cause: risk-engine v2.8.4 (first_onset_node=risk-node-02, span=acquire_rule_lock).\n"
    f"Fix merged to main: {merged_sha}\n"
    f"Canary run fingerprint: {canary_fp}\n"
    f"Total audited SLA penalty: {float(q2(total_penalty)):.2f} CNY.\n",
    "plain", "utf-8",
)
msg["From"] = "sre_oncall@clearing.local"
msg["To"] = "sre-lead@clearing.local"
msg["Subject"] = f"[Postmortem] risk-engine fix {merged_sha[:8]} ({canary_fp}) & SLA Audit"
with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
    s.sendmail("sre_oncall@clearing.local", ["sre-lead@clearing.local"], msg.as_string())

print(f"[solve] DONE: {merged_sha[:8]} fp={canary_fp} penalty={float(q2(total_penalty))}", flush=True)
