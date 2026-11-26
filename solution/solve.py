#!/usr/bin/env python3
"""v0.4.0 Reference solution — browser-based operations via root Chromium CDP.

Terminal: git clone/push, pytest, SLA calculation, output files, email.
Browser (Playwright → CDP :9222): Gitea PR closure, Portal isolate/change/canary/approve/audit.
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
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP

from playwright.sync_api import sync_playwright

TZ_UTC8 = dt.timezone(dt.timedelta(hours=8))
GITEA = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
GITEA_AUTH = ("sre_oncall", "Clearing!2026")
PORTAL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8080")
SMTP_HOST = os.environ.get("MAILPIT_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))
CDP_ENDPOINT = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
WORKSPACE_DIR = pathlib.Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
OUTPUT_DIR = WORKSPACE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def q2(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Phase 1: Terminal — git clone, fix code, pytest, push to main ──

fix_sha = None
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

print("[solve] Phase 1 complete: git push done, fix_sha=", fix_sha, flush=True)

# ── Phase 2: Terminal — SLA calculation from local data files ──

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

print("[solve] Phase 2 complete: SLA calc done, total_penalty=", float(q2(total_penalty)), flush=True)

# ── Phase 3: Browser — Gitea PR closure + Portal operations via CDP ──

import time as _time

def _connect_cdp(playwright, endpoint, retries=10, delay=2):
    for i in range(retries):
        try:
            return playwright.chromium.connect_over_cdp(endpoint)
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"[solve] CDP connect attempt {i+1}/{retries} failed: {e}", flush=True)
            _time.sleep(delay)
    raise RuntimeError("CDP connect exhausted retries")

with sync_playwright() as p:
    print("[solve] sync_playwright started, connecting CDP...", flush=True)
    browser = _connect_cdp(p, CDP_ENDPOINT)
    print("[solve] CDP connected, creating context...", flush=True)
    context = browser.new_context()
    page = context.new_page()
    print("[solve] page created, navigating to Gitea PR...", flush=True)

    # 3a. Gitea: close PR #1 via web UI (non-blocking — don't let it hang the whole solve)
    try:
        page.goto(f"{GITEA}/clearing-core/risk-engine/pulls/1", timeout=10000)
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        close_btn = page.locator('button:has-text("Close"), button:has-text("关闭"), [data-content="Close PR"]')
        if close_btn.count() > 0:
            close_btn.first.click(timeout=5000)
            page.wait_for_timeout(1000)
        print("[solve] Gitea PR close attempted", flush=True)
    except Exception as e:
        print(f"[solve] Gitea PR close skipped: {e}", flush=True)

    # 3b. Portal: login
    print("[solve] Portal login...", flush=True)
    page.goto(f"{PORTAL}/login", timeout=10000)
    page.wait_for_load_state("domcontentloaded", timeout=5000)
    page.fill('input[name="username"]', "sre_oncall")
    page.fill('input[name="password"]', "Clearing!2026")
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")

    # 3c. Portal: isolate risk-node-02 and risk-node-03
    page.click('button.tab:has-text("CMDB")')
    page.wait_for_timeout(500)
    for node in ("risk-node-02", "risk-node-03"):
        page.click(f'[data-node="{node}"]')
        page.wait_for_timeout(200)
        page.click(f'button[onclick*="ISOLATED"]')
        page.wait_for_timeout(500)

    # 3d. Portal: create change ticket
    page.click('button.tab:has-text("变更")')
    page.wait_for_timeout(500)
    page.fill('#chg-svc', 'risk-engine')
    page.fill('#chg-sha', fix_sha)
    page.fill('#chg-span', 'acquire_rule_lock')
    page.fill('#chg-win', '2026-11-24 19:45-20:15')
    page.fill('#chg-desc', 'Hotfix lock contention around FX quote refresh and idempotency key collision')
    page.click('button:has-text("创建变更单")')
    page.wait_for_timeout(1000)

    # 3e. Portal: trigger canary replay on the new ticket
    replay_btn = page.locator('button[onclick*="runReplay"]').last
    replay_btn.click()
    page.wait_for_timeout(5000)  # canary replay takes a few seconds

    # 3f. Portal: approve the change
    approve_btn = page.locator('button[onclick*="approveChange"]').last
    approve_btn.click()
    page.wait_for_timeout(1000)

    # 3g. Portal: audit each batch
    page.click('button.tab:has-text("SLA")')
    page.wait_for_timeout(500)
    for r in summary_rows:
        bid = r["batch_id"]
        page.fill(f'#bs-{bid}', str(r["settled_amount_cny"]))
        page.fill(f'#bb-{bid}', str(r["breached_tx_count"]))
        page.fill(f'#bd-{bid}', str(r["duplicate_risk_count"]))
        page.fill(f'#bp-{bid}', str(r["penalty_cny"]))
        page.click(f'button[onclick*="saveBatch(\'{bid}\')"]')
        page.wait_for_timeout(500)

    browser.close()

# ── Phase 4: Terminal — write postmortem + send email ──

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

msg = email.mime.text.MIMEText(
    f"Root cause service: risk-engine (v2.8.4 onset 2026-11-24T14:14:00+08:00, span acquire_rule_lock).\n"
    f"Gitea main fix commit (Canary PASSED): {fix_sha}\n"
    f"Total audited SLA penalty: {float(q2(total_penalty)):.2f} CNY.\n",
    "plain",
    "utf-8",
)
msg["From"] = "sre_oncall@clearing.local"
msg["To"] = "sre-lead@clearing.local"
msg["Subject"] = f"[Postmortem Complete] risk-engine fix {fix_sha[:8]} & SLA Audit"
with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
    s.sendmail("sre_oncall@clearing.local", ["sre-lead@clearing.local"], msg.as_string())

print("v0.4.0 Oracle solve complete:", fix_sha, float(q2(total_penalty)))
