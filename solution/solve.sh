#!/usr/bin/env bash
set -euo pipefail

python3 - << 'PYEOF'
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
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP

import requests

TZ_UTC8 = dt.timezone(dt.timedelta(hours=8))
GITEA = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
GITEA_AUTH = ("sre_oncall", "Clearing!2026")
PORTAL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8080")
SMTP_HOST = os.environ.get("MAILPIT_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))
WORKSPACE_DIR = pathlib.Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
OUTPUT_DIR = WORKSPACE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# 1. Close trap PR #1 on Gitea with DBA constraint comment
requests.post(
    f"{GITEA}/api/v1/repos/clearing-core/risk-engine/issues/1/comments",
    auth=GITEA_AUTH,
    json={"body": "Rejecting PR #1: pg-primary-01 max_connections=180 (168 active). Raising DB_POOL_SIZE to 300 will exhaust DB slots."},
    timeout=10,
).raise_for_status()
requests.patch(
    f"{GITEA}/api/v1/repos/clearing-core/risk-engine/pulls/1",
    auth=GITEA_AUTH,
    json={"state": "closed"},
    timeout=10,
).raise_for_status()

# 2. Clone risk-engine, fix lock contention + 503 cache fallback + tx_id idempotency key, run pytest, push main
with tempfile.TemporaryDirectory() as td:
    parsed_gitea = urllib.parse.urlparse(GITEA)
    clone_url = f"{parsed_gitea.scheme}://sre_oncall:{urllib.parse.quote('Clearing!2026', safe='')}@{parsed_gitea.netloc}/clearing-core/risk-engine.git"
    subprocess.run(["git", "clone", clone_url, td], check=True)
    subprocess.run(["git", "config", "user.name", "sre_oncall"], cwd=td, check=True)
    subprocess.run(["git", "config", "user.email", "sre-oncall@clearing.local"], cwd=td, check=True)

    (pathlib.Path(td) / "risk_engine" / "engine.py").write_text('''import hashlib
import threading
import time
from typing import Dict

from risk_engine.fx_client import FxClient


class RiskEngine:
    def __init__(self, fx_client: FxClient | None = None, cache_ttl: float = 30.0):
        self.fx_client = fx_client or FxClient()
        self.cache_ttl = cache_ttl
        self._rule_cache_lock = threading.Lock()
        self._fx_cache: Dict[str, tuple[float, float]] = {
            "USD_CNY": (7.15, time.monotonic()),
            "HKD_CNY": (0.92, time.monotonic()),
        }

    def generate_idempotency_key(self, tx_id: str, merchant_id: str, cycle_time: str) -> str:
        raw = f"{merchant_id}:{tx_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def get_fx_rate(self, pair: str) -> float:
        if pair == "CNY_CNY":
            return 1.0
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
from risk_engine.engine import RiskEngine
from risk_engine.fx_client import FxClient


def test_basic_evaluation():
    eng = RiskEngine(fx_client=FxClient(lambda p: 7.15))
    res = eng.evaluate_tx("TX-1", "MCH_VIP_001", "USD", 100.0, "2026-11-24T14:15:01+08:00")
    assert res["amount_cny"] == 715.0
    assert res["risk_pass"] is True


def test_idempotency_unique_per_tx():
    eng = RiskEngine(fx_client=FxClient(lambda p: 7.15))
    assert eng.generate_idempotency_key("TX-A", "M1", "2026-11-24T14:15:01+08:00") != eng.generate_idempotency_key("TX-B", "M1", "2026-11-24T14:15:45+08:00")


def test_non_blocking_fx_refresh():
    def slow_fetch(pair):
        if pair == "USD_CNY":
            time.sleep(0.3)
            return 7.15
        return 0.92
    eng = RiskEngine(fx_client=FxClient(slow_fetch), cache_ttl=30.0)
    eng._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 100.0)
    eng._fx_cache["HKD_CNY"] = (0.92, time.monotonic())
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f = ex.submit(eng.evaluate_tx, "TX-1", "M1", "USD", 10.0, "2026-11-24T14:15:01+08:00")
        time.sleep(0.05)
        t0 = time.monotonic()
        r2 = eng.evaluate_tx("TX-2", "M1", "HKD", 10.0, "2026-11-24T14:15:02+08:00")
        assert (time.monotonic() - t0) < 0.2
        assert r2["amount_cny"] == 9.2
        f.result(timeout=3)
''', encoding="utf-8")

    subprocess.run(["pytest", "-q"], cwd=td, check=True)
    subprocess.run(["git", "add", "."], cwd=td, check=True)
    subprocess.run(["git", "commit", "-m", "fix: release _rule_cache_lock during FX HTTP refresh and key by tx_id"], cwd=td, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=td, check=True)
    fix_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=td, text=True).strip()

# 3. Clean logs & compute SLA penalties (REV-2 Errata + MCH_VIP_003 Clause 4.2 00:00-00:59 UTC+8 exemption!)
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

        # Clause 4.2: MCH_VIP_003 between 00:00:00 and 00:59:59 UTC+8 exempt from SLA latency breach/penalty
        in_maint_exempt = (merchant_id == "MCH_VIP_003") and (latest_ts.hour == 0)
        threshold = 600 if tier == "VIP_T1" else 1500
        breached = (not in_maint_exempt) and (
            (latest["status"] in ("TIMEOUT", "504", "FAILED")) or (int(latest["latency_ms"]) > threshold)
        )

        b = batches.setdefault(bid, {
            "batch_id": bid, "merchant_id": merchant_id, "tier": tier, "currency": cur,
            "settled_amount_cny": Decimal("0.00"), "breached_tx_count": 0,
            "duplicate_risk_count": 0, "raw_penalty": Decimal("0.00"),
        })
        b["settled_amount_cny"] += amt_cny
        if breached:
            b["breached_tx_count"] += 1
            rate = Decimal("0.025") if tier == "VIP_T1" else Decimal("0.005")
            b["raw_penalty"] += amt_cny * rate
        if is_dup:
            b["duplicate_risk_count"] += 1

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

# 4. Browser session login + CSRF interaction with Portal
sess = requests.Session()
sess.post(f"{PORTAL}/login", data={"username": "sre_oncall", "password": "Clearing!2026"}, timeout=10).raise_for_status()
home_html = sess.get(f"{PORTAL}/", timeout=10).text
m_csrf = re.search(r'const CSRF = "([0-9a-f]+)"', home_html)
assert m_csrf, "Failed extracting CSRF token from Portal page"
csrf_hdr = {"X-CSRF-Token": m_csrf.group(1)}

for node in ("risk-node-02", "risk-node-03"):
    sess.put(f"{PORTAL}/api/nodes/{node}", headers=csrf_hdr, json={"status": "ISOLATED"}, timeout=10).raise_for_status()

chg_r = sess.post(
    f"{PORTAL}/api/changes",
    headers=csrf_hdr,
    json={
        "service": "risk-engine",
        "commit_sha": fix_sha,
        "bottleneck_span": "acquire_rule_lock",
        "scheduled_window_utc8": "2026-11-24 19:45-20:15",
        "description": "Hotfix lock contention around FX quote refresh and idempotency key collision",
    },
    timeout=10,
)
chg_r.raise_for_status()
ticket_id = chg_r.json()["ticket_id"]

# Trigger Live Canary Replay and verify CANARY_PASSED before approving
rep_r = sess.post(f"{PORTAL}/api/changes/{ticket_id}/canary-replay", headers=csrf_hdr, timeout=30)
rep_r.raise_for_status()
assert rep_r.json()["canary_status"] == "CANARY_PASSED", rep_r.text

sess.post(f"{PORTAL}/api/changes/{ticket_id}/approve", headers=csrf_hdr, timeout=10).raise_for_status()

for r in summary_rows:
    sess.put(
        f"{PORTAL}/api/batches/{r['batch_id']}",
        headers=csrf_hdr,
        json={
            "settled_amount_cny": r["settled_amount_cny"],
            "breached_tx_count": r["breached_tx_count"],
            "duplicate_risk_count": r["duplicate_risk_count"],
            "penalty_cny": r["penalty_cny"],
            "audit_status": "AUDITED",
            "notes": "Audited per REV-2 & Contract Clause 4.2",
        },
        timeout=10,
    ).raise_for_status()

# 5. Write incident_postmortem.json
postmortem = {
    "root_cause_service": "risk-engine",
    "onset_time_utc8": "2026-11-24T14:14:00+08:00",
    "culprit_version": "v2.8.4",
    "bottleneck_span": "acquire_rule_lock",
    "gitea_fix_commit": fix_sha,
    "isolated_nodes": ["risk-node-02", "risk-node-03"],
    "scheduled_release_window_utc8": "2026-11-24 19:45-20:15",
    "total_penalty_cny": float(q2(total_penalty)),
    "batches": {r["batch_id"]: r for r in summary_rows},
    "duplicate_risk_tx_ids": sorted(dup_risk_ids),
}
(OUTPUT_DIR / "incident_postmortem.json").write_text(json.dumps(postmortem, indent=2, ensure_ascii=False), encoding="utf-8")

# 6. Send summary email to sre-lead@clearing.local
msg = email.mime.text.MIMEText(
    f"Root cause service: risk-engine (v2.8.4 onset 2026-11-24T14:14:00+08:00, span acquire_rule_lock).\n"
    f"Gitea main fix commit (Canary PASSED): {fix_sha}\n"
    f"Total audited SLA penalty: {float(q2(total_penalty)):.2f} CNY.\n",
    "plain",
    "utf-8",
)
msg["From"] = "sre-oncall@clearing.local"
msg["To"] = "sre-lead@clearing.local"
msg["Subject"] = f"[Postmortem Complete] risk-engine fix {fix_sha[:8]} & SLA Audit"
with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
    s.sendmail("sre-oncall@clearing.local", ["sre-lead@clearing.local"], msg.as_string())

print("v2 Oracle solve complete:", fix_sha, float(q2(total_penalty)))
PYEOF
