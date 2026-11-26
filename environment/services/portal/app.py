#!/usr/bin/env python3
"""v2 CMDB Spatial SVG Topology + Live Gitea Canary Replay Simulator + SLA Audit Console."""
import concurrent.futures
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from pftrace_encoder import PftraceWriter

DB_PATH = pathlib.Path(os.environ.get("PORTAL_DB_PATH", "/var/lib/clearing-portal/portal.db"))
GITEA_URL = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
GITEA_USER = "sre_oncall"
GITEA_PASS = "Clearing!2026"
TRACE_DIR = pathlib.Path(os.environ.get("TRACE_DIR", "/workspace/traces"))
AUTH_USER = "sre_oncall"
AUTH_PASS = "Clearing!2026"
SESSION_SECRET = os.environ.get("PORTAL_SESSION_SECRET", "clearing-portal-secret-2026-v2")

app = FastAPI(title="Clearing CMDB, Canary Gate & SLA Audit Console")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(reset: bool = False) -> None:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS cmdb_nodes (
            node_name TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            zone TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            p99_ms INTEGER NOT NULL,
            queue_depth INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS change_tickets (
            ticket_id TEXT PRIMARY KEY,
            service TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            bottleneck_span TEXT NOT NULL,
            scheduled_window_utc8 TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            canary_status TEXT NOT NULL DEFAULT 'NOT_RUN',
            replay_p99_ms REAL,
            replay_collisions INTEGER,
            replay_pool_size INTEGER,
            replay_summary TEXT NOT NULL DEFAULT '',
            locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sla_batches (
            batch_id TEXT PRIMARY KEY,
            batch_date TEXT NOT NULL,
            merchant_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            currency TEXT NOT NULL,
            settled_amount_cny REAL,
            breached_tx_count INTEGER,
            duplicate_risk_count INTEGER,
            penalty_cny REAL,
            audit_status TEXT NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS live_canary_metrics (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            replay_count INTEGER NOT NULL DEFAULT 0,
            last_status INTEGER NOT NULL DEFAULT 0,
            last_p99_ms REAL NOT NULL DEFAULT 0.0,
            last_pg_conns INTEGER NOT NULL DEFAULT 42,
            last_collisions INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    if cur.execute("SELECT COUNT(*) FROM cmdb_nodes").fetchone()[0] == 0:
        nodes = [
            ("risk-node-01", "risk-engine", "zone-a", "canary", "ACTIVE", 86, 2),
            ("risk-node-02", "risk-engine", "zone-b", "worker", "DEGRADED", 1540, 44),
            ("risk-node-03", "risk-engine", "zone-b", "worker", "DEGRADED", 1495, 39),
            ("settle-node-01", "settlement-worker", "zone-a", "primary", "ACTIVE", 920, 18),
            ("settle-node-02", "settlement-worker", "zone-b", "standby", "ACTIVE", 145, 3),
            ("pg-primary-01", "postgres", "core-db", "primary_db", "ACTIVE", 210, 168),
        ]
        cur.executemany("INSERT INTO cmdb_nodes VALUES (?,?,?,?,?,?,?)", nodes)

    if cur.execute("SELECT COUNT(*) FROM change_tickets").fetchone()[0] == 0:
        tickets = [
            ("CHG-20261123-01", "settlement-worker", "a1b2c3d4", "none", "2026-11-23 15:00-15:30", "Routine connection tuning", "EXECUTED", "CANARY_PASSED", 92.0, 0, 120, "Historical passed", 1),
            ("CHG-20261124-01", "risk-engine", "v2.8.4", "unknown", "2026-11-24 14:10-14:15", "Deploy FX cache refresh v2.8.4", "EXECUTED", "NOT_RUN", None, None, None, "Bypassed canary", 1),
        ]
        cur.executemany("INSERT INTO change_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tickets)

    if cur.execute("SELECT COUNT(*) FROM sla_batches").fetchone()[0] == 0:
        batches = [
            ("BATCH-20261122-01", "2026-11-22", "MCH_VIP_001", "VIP_T1", "USD", 125000.00, 0, 0, 0.00, "LOCKED", 1, "Archived historical cycle"),
            ("BATCH-20261123-01", "2026-11-23", "MCH_VIP_002", "VIP_T1", "CNY", 310000.00, 1, 0, 185.00, "LOCKED", 1, "Archived historical cycle"),
            ("BATCH-20261123-02", "2026-11-23", "MCH_STD_010", "STD", "HKD", 84200.00, 0, 0, 0.00, "LOCKED", 1, "Archived historical cycle"),
            ("BATCH-20261124-01", "2026-11-24", "MCH_VIP_001", "VIP_T1", "USD", None, None, None, None, "PENDING", 0, "Awaiting 2026-11-24 audit"),
            ("BATCH-20261124-02", "2026-11-24", "MCH_VIP_002", "VIP_T1", "CNY", None, None, None, None, "PENDING", 0, "Awaiting 2026-11-24 audit"),
            ("BATCH-20261124-03", "2026-11-24", "MCH_STD_010", "STD", "HKD", None, None, None, None, "PENDING", 0, "Awaiting 2026-11-24 audit"),
            ("BATCH-20261124-04", "2026-11-24", "MCH_VIP_003", "VIP_T1", "USD", None, None, None, None, "PENDING", 0, "Awaiting 2026-11-24 audit"),
        ]
        cur.executemany("INSERT INTO sla_batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", batches)

    if cur.execute("SELECT COUNT(*) FROM live_canary_metrics").fetchone()[0] == 0:
        cur.execute("INSERT INTO live_canary_metrics VALUES (1, 0, 0, 1520.0, 168, 4)")

    conn.commit()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_db(reset=False)


def make_session_token(user: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), user.encode(), "sha256").hexdigest()
    return f"{user}:{sig}"


def make_csrf_token(session_token: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), f"csrf:{session_token}".encode(), "sha256").hexdigest()[:32]


def verify_session(request: Request, require_csrf: bool = False) -> str:
    tok = request.cookies.get("portal_session", "")
    expected = make_session_token(AUTH_USER)
    if not tok or not hmac.compare_digest(tok, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Browser session login required at /login")
    if require_csrf:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(csrf, make_csrf_token(tok)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing or invalid X-CSRF-Token")
    return AUTH_USER


def require_read(request: Request) -> str:
    return verify_session(request, require_csrf=False)


def require_write(request: Request) -> str:
    return verify_session(request, require_csrf=True)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    conn = get_conn()
    r = dict(conn.execute("SELECT * FROM live_canary_metrics WHERE id=1").fetchone())
    conn.close()
    return "\n".join([
        "# TYPE clearing_canary_replay_count counter",
        f"clearing_canary_replay_count {r['replay_count']}",
        "# TYPE clearing_canary_replay_status gauge",
        f"clearing_canary_replay_status {r['last_status']}",
        "# TYPE clearing_canary_replay_p99_ms gauge",
        f"clearing_canary_replay_p99_ms {r['last_p99_ms']}",
        "# TYPE clearing_canary_pg_conns gauge",
        f"clearing_canary_pg_conns {r['last_pg_conns']}",
        "# TYPE clearing_canary_idem_collisions gauge",
        f"clearing_canary_idem_collisions {r['last_collisions']}",
        "",
    ])


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return """<!doctype html><html><head><meta charset="utf-8"><title>Login - Clearing Console</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e293b;padding:32px;border-radius:12px;width:360px;box-shadow:0 10px 30px rgba(0,0,0,.4)}
input{width:100%;padding:10px;margin:8px 0 16px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#fff;box-sizing:border-box}
button{width:100%;padding:10px;border:none;border-radius:6px;background:#2563eb;color:#fff;font-weight:600;cursor:pointer}</style></head>
<body><form class="card" method="post" action="/login">
<h2>CMDB, 灰度验证 & SLA 审计台</h2>
<label>Username</label><input name="username" value="sre_oncall" required>
<label>Password</label><input type="password" name="password" required>
<button type="submit">Sign In</button></form></body></html>"""


@app.post("/login")
def do_login(username: str = Form(...), password: str = Form(...)):
    if username == AUTH_USER and password == AUTH_PASS:
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie("portal_session", make_session_token(username), httponly=True, samesite="lax")
        return resp
    raise HTTPException(status_code=401, detail="Invalid credentials")


class NodeUpdate(BaseModel):
    status: str


class ChangeCreate(BaseModel):
    ticket_id: Optional[str] = None
    service: str
    commit_sha: str
    bottleneck_span: str
    scheduled_window_utc8: str
    description: str


class BatchAuditUpdate(BaseModel):
    settled_amount_cny: float
    breached_tx_count: int
    duplicate_risk_count: int
    penalty_cny: float
    audit_status: str = "AUDITED"
    notes: Optional[str] = ""


def run_gitea_canary_replay(commit_sha: str) -> dict:
    """Clone agent's Gitea risk-engine repo, verify commit/pool, run live concurrent + 503 stress replay,
    emit dynamic .pftrace file, and return real telemetry.
    """
    parsed = urllib.parse.urlparse(GITEA_URL)
    u = urllib.parse.quote(GITEA_USER, safe="")
    p = urllib.parse.quote(GITEA_PASS, safe="")
    clone_url = f"{parsed.scheme}://{u}:{p}@{parsed.netloc}/clearing-core/risk-engine.git"

    with tempfile.TemporaryDirectory() as td:
        cp = subprocess.run(["git", "clone", "--depth", "10", clone_url, td], capture_output=True, text=True, timeout=20)
        if cp.returncode != 0:
            return {"passed": False, "p99_ms": 2500.0, "collisions": 5, "pool_size": 180, "summary": f"git clone error: {cp.stderr}"}
        # Checkout requested commit_sha if present on repo
        co = subprocess.run(["git", "checkout", commit_sha], cwd=td, capture_output=True, text=True, timeout=10)
        if co.returncode != 0:
            return {"passed": False, "p99_ms": 2500.0, "collisions": 5, "pool_size": 180, "summary": f"Commit {commit_sha} not found on Gitea risk-engine"}

        cfg_path = pathlib.Path(td) / "risk_engine" / "config.py"
        pool_size = 120
        if cfg_path.exists():
            m = re.search(r"DB_POOL_SIZE\s*=\s*(\d+)", cfg_path.read_text(encoding="utf-8"))
            if m:
                pool_size = int(m.group(1))

        stress_code = r'''
import concurrent.futures, json, sys, time
sys.path.insert(0, ".")
from risk_engine.engine import RiskEngine
from risk_engine.fx_client import FxClient

def slow_flaky(pair):
    if pair == "USD_CNY":
        time.sleep(0.35)
        raise RuntimeError("503 upstream timeout")
    return 0.92

eng = RiskEngine(fx_client=FxClient(slow_flaky), cache_ttl=30.0)
eng._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 100.0)
eng._fx_cache["HKD_CNY"] = (0.92, time.monotonic())

latencies = []
errors = 0
keys = set()

def worker(i):
    t0 = time.monotonic()
    pair_cur = "USD" if i == 0 else "HKD"
    res = eng.evaluate_tx(f"TX-REPLAY-{i}", "MCH_VIP_001", pair_cur, 100.0, "2026-11-24T19:45:05+08:00")
    return (time.monotonic() - t0) * 1000.0, res["idempotency_key"]

with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    futs = [ex.submit(worker, i) for i in range(6)]
    for f in futs:
        try:
            ms, k = f.result(timeout=4)
            latencies.append(ms)
            keys.add(k)
        except Exception:
            errors += 1

hkd_max = max(latencies[1:]) if len(latencies) > 1 else 2500.0
collisions = 6 - len(keys)
print(json.dumps({"hkd_max_ms": round(hkd_max, 2), "errors": errors, "collisions": collisions}))
'''
        rp = subprocess.run([sys.executable, "-c", stress_code], cwd=td, capture_output=True, text=True, timeout=15)
        if rp.returncode != 0:
            res = {"hkd_max_ms": 2400.0, "errors": 6, "collisions": 5}
        else:
            res = json.loads(rp.stdout.strip().splitlines()[-1])

    p99_ms = float(res["hkd_max_ms"])
    errors = int(res["errors"])
    collisions = int(res["collisions"])
    passed = (pool_size <= 180) and (p99_ms < 180.0) and (errors == 0) and (collisions == 0)

    # Emit live canary .pftrace for Perfetto inspection
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        pw = PftraceWriter(seq_id=3001)
        pw.add_process_track(100, 901, f"canary-replay ({commit_sha[:8]})")
        for i in range(6):
            pw.add_thread_track(200 + i, 100, 901, 10 + i, f"replay-worker-{i}")
            ts = 1_000_000_000 + i * 5_000_000
            if passed:
                pw.add_slice(200 + i, ts, 12_000_000, "RiskEngine::evaluate_tx", "risk", {"status": "PASS", "p99_ms": int(p99_ms)})
            else:
                pw.add_slice(200 + i, ts, 450_000_000, "acquire_rule_lock", "lock", {"status": "BLOCKED", "pool_size": pool_size})
        (TRACE_DIR / "canary_replay_latest.pftrace").write_bytes(pw.to_bytes())
    except Exception:
        pass

    summary = f"pool={pool_size}/180, hkd_concurrent_max={p99_ms:.1f}ms, errors={errors}, idem_collisions={collisions}"
    return {"passed": passed, "p99_ms": p99_ms, "collisions": collisions, "pool_size": pool_size, "summary": summary}


@app.get("/api/nodes")
def list_nodes(_: str = Depends(require_read)):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM cmdb_nodes ORDER BY node_name")]
    conn.close()
    return {"nodes": rows}


@app.put("/api/nodes/{node_name}")
def update_node(node_name: str, payload: NodeUpdate, _: str = Depends(require_write)):
    allowed = {"ACTIVE", "ISOLATED", "DRAINED", "DEGRADED"}
    if payload.status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status, expected one of {allowed}")
    conn = get_conn()
    row = conn.execute("SELECT * FROM cmdb_nodes WHERE node_name=?", (node_name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Node not found")
    conn.execute("UPDATE cmdb_nodes SET status=? WHERE node_name=?", (payload.status, node_name))
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM cmdb_nodes WHERE node_name=?", (node_name,)).fetchone())
    conn.close()
    return updated


@app.get("/api/changes")
def list_changes(_: str = Depends(require_read)):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM change_tickets ORDER BY ticket_id")]
    conn.close()
    return {"changes": rows}


@app.post("/api/changes")
def create_change(payload: ChangeCreate, _: str = Depends(require_write)):
    conn = get_conn()
    tid = payload.ticket_id
    if not tid:
        cnt = conn.execute("SELECT COUNT(*) FROM change_tickets").fetchone()[0] + 1
        tid = f"CHG-20261124-{cnt:02d}"
    existing = conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (tid,)).fetchone()
    if existing and existing["locked"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Historical change ticket is LOCKED")
    if existing:
        conn.execute(
            """UPDATE change_tickets SET service=?, commit_sha=?, bottleneck_span=?, scheduled_window_utc8=?,
               description=?, status='DRAFT', canary_status='NOT_RUN' WHERE ticket_id=?""",
            (payload.service, payload.commit_sha, payload.bottleneck_span, payload.scheduled_window_utc8, payload.description, tid),
        )
    else:
        conn.execute(
            "INSERT INTO change_tickets VALUES (?,?,?,?,?,?,'DRAFT','NOT_RUN',NULL,NULL,NULL,'',0)",
            (tid, payload.service, payload.commit_sha, payload.bottleneck_span, payload.scheduled_window_utc8, payload.description),
        )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (tid,)).fetchone())
    conn.close()
    return row


@app.post("/api/changes/{ticket_id}/canary-replay")
def trigger_canary_replay(ticket_id: str, _: str = Depends(require_write)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["locked"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Ticket is LOCKED")

    res = run_gitea_canary_replay(row["commit_sha"])
    c_status = "CANARY_PASSED" if res["passed"] else "CANARY_FAILED"
    conn.execute(
        """UPDATE change_tickets SET canary_status=?, replay_p99_ms=?, replay_collisions=?, replay_pool_size=?, replay_summary=?
           WHERE ticket_id=?""",
        (c_status, res["p99_ms"], res["collisions"], res["pool_size"], res["summary"], ticket_id),
    )
    pg_sim = min(180, res["pool_size"]) if res["passed"] else max(180, res["pool_size"])
    conn.execute(
        """UPDATE live_canary_metrics SET replay_count=replay_count+1, last_status=?, last_p99_ms=?, last_pg_conns=?, last_collisions=?
           WHERE id=1""",
        (1 if res["passed"] else 0, res["p99_ms"], pg_sim, res["collisions"]),
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
    conn.close()
    return updated


@app.post("/api/changes/{ticket_id}/approve")
def approve_change(ticket_id: str, _: str = Depends(require_write)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["locked"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Ticket is LOCKED")
    if row["canary_status"] != "CANARY_PASSED":
        conn.close()
        raise HTTPException(status_code=409, detail="Canary Replay must pass (CANARY_PASSED) before approving release")
    conn.execute("UPDATE change_tickets SET status='APPROVED' WHERE ticket_id=?", (ticket_id,))
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
    conn.close()
    return updated


@app.get("/api/batches")
def list_batches(_: str = Depends(require_read)):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM sla_batches ORDER BY batch_id")]
    conn.close()
    return {"batches": rows}


@app.put("/api/batches/{batch_id}")
def update_batch(batch_id: str, payload: BatchAuditUpdate, _: str = Depends(require_write)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sla_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Batch not found")
    if row["locked"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Batch is LOCKED and cannot be modified")
    conn.execute(
        """UPDATE sla_batches
           SET settled_amount_cny=?, breached_tx_count=?, duplicate_risk_count=?, penalty_cny=?, audit_status=?, notes=?
           WHERE batch_id=?""",
        (round(payload.settled_amount_cny, 2), payload.breached_tx_count, payload.duplicate_risk_count, round(payload.penalty_cny, 2), payload.audit_status, payload.notes or "", batch_id),
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM sla_batches WHERE batch_id=?", (batch_id,)).fetchone())
    conn.close()
    return updated


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    try:
        verify_session(request, require_csrf=False)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=302)
    csrf_tok = make_csrf_token(request.cookies.get("portal_session", ""))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><title>CMDB 空间拓扑、灰度验证 & SLA 审计台</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f1f5f9;margin:0;color:#0f172a}}
header{{background:#0f172a;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}}
.tabs{{display:flex;gap:8px;padding:12px 24px;background:#fff;border-bottom:1px solid #cbd5e1}}
.tab{{padding:8px 16px;border-radius:6px;border:1px solid #cbd5e1;background:#f8fafc;cursor:pointer;font-weight:600}}
.tab.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
.container{{padding:24px;max-width:1280px;margin:0 auto}}
.panel{{display:none;background:#fff;padding:20px;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.panel.active{{display:block}}
.topo-wrap{{display:grid;grid-template-columns:2fr 1fr;gap:20px;align-items:start}}
.inspector{{background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;padding:16px}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{border:1px solid #e2e8f0;padding:8px;text-align:left}}
th{{background:#f8fafc}}
.badge{{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;display:inline-block}}
.ACTIVE,.CANARY_PASSED,.APPROVED{{background:#dcfce7;color:#166534}}
.DEGRADED,.CANARY_FAILED{{background:#fee2e2;color:#991b1b}}
.ISOLATED,.DRAINED,.PENDING{{background:#fef3c7;color:#92400e}}
.LOCKED{{background:#e2e8f0;color:#475569}}
.AUDITED,.DRAFT{{background:#dbeafe;color:#1e40af}}
input,select{{padding:6px 8px;border:1px solid #cbd5e1;border-radius:5px}}
button{{padding:6px 12px;border:none;border-radius:5px;background:#2563eb;color:#fff;cursor:pointer;font-size:13px}}
button.warn{{background:#d97706}} button.ok{{background:#16a34a}}
button:disabled{{background:#94a3b8;cursor:not-allowed}}
.form-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:12px}}
svg text{{user-select:none}}
.node-g{{cursor:pointer}}
</style>
</head>
<body>
<header><strong>跨境清结算平台 — CMDB 空间拓扑 / 灰度门禁 / SLA 审计控制台</strong><span>sre_oncall</span></header>
<div class="tabs">
  <button class="tab active" onclick="showTab('cmdb',this)">1. CMDB 空间拓扑图</button>
  <button class="tab" onclick="showTab('changes',this)">2. 变更发布与灰度回放门禁</button>
  <button class="tab" onclick="showTab('sla',this)">3. SLA 赔付审计台</button>
</div>
<div class="container">
  <div id="cmdb" class="panel active">
    <h3>集群服务空间拓扑（点击节点查看详情并操作）</h3>
    <div class="topo-wrap">
      <svg id="topo-svg" viewBox="0 0 820 420" style="width:100%;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc">
        <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 1 L 10 5 L 0 9 z" fill="#64748b"/></marker></defs>
        <rect x="20" y="20" width="240" height="380" rx="10" fill="#eef2ff" stroke="#c7d2fe"/>
        <text x="34" y="44" font-size="13" font-weight="700" fill="#3730a3">ZONE-A (Edge & Canary)</text>
        <rect x="285" y="20" width="265" height="380" rx="10" fill="#fff7ed" stroke="#fed7aa"/>
        <text x="299" y="44" font-size="13" font-weight="700" fill="#9a3412">ZONE-B (Compute Pool)</text>
        <rect x="575" y="20" width="225" height="380" rx="10" fill="#f0fdf4" stroke="#bbf7d0"/>
        <text x="589" y="44" font-size="13" font-weight="700" fill="#166534">CORE DB ZONE</text>
        <line x1="210" y1="120" x2="315" y2="120" stroke="#64748b" stroke-width="2" marker-end="url(#arr)"/>
        <line x1="210" y1="120" x2="315" y2="235" stroke="#64748b" stroke-width="2" marker-end="url(#arr)"/>
        <line x1="210" y1="260" x2="315" y2="330" stroke="#64748b" stroke-width="2" marker-end="url(#arr)"/>
        <line x1="495" y1="120" x2="600" y2="195" stroke="#64748b" stroke-width="2" marker-end="url(#arr)"/>
        <line x1="495" y1="235" x2="600" y2="205" stroke="#64748b" stroke-width="2" marker-end="url(#arr)"/>
        <line x1="495" y1="330" x2="600" y2="220" stroke="#64748b" stroke-width="2" marker-end="url(#arr)"/>
        <g id="svg-nodes"></g>
      </svg>
      <div class="inspector" id="node-inspector">
        <h4>节点操作面板</h4>
        <p style="color:#64748b;font-size:13px">请在左侧拓扑图点击任意节点查看实时 P99 与队列状态。</p>
      </div>
    </div>
  </div>
  <div id="changes" class="panel">
    <h3>变更发布单与 Gitea 灰度压测回放门禁</h3>
    <p style="font-size:13px;color:#475569">提交变更单后须点击「触发灰度回放」拉取 Gitea 代码执行并发/503 压测并同步刷新 Grafana 与 Perfetto (<code>/traces/canary_replay_latest.pftrace</code>)，通过后方可「签发排期」。</p>
    <table id="changes-table"><thead><tr><th>单号</th><th>服务</th><th>Commit</th><th>Span</th><th>窗口 (UTC+8)</th><th>灰度门禁</th><th>回放遥测</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody></table>
    <h4 style="margin-top:18px">新建变更单 (DRAFT)</h4>
    <div class="form-grid">
      <input id="chg-svc" placeholder="service (e.g. risk-engine)">
      <input id="chg-sha" placeholder="Gitea main commit SHA">
      <input id="chg-span" placeholder="bottleneck_span">
      <input id="chg-win" placeholder="YYYY-MM-DD HH:MM-HH:MM">
      <input id="chg-desc" placeholder="变更说明" style="grid-column:span 2">
    </div>
    <button style="margin-top:12px" onclick="createChange()">创建变更单</button>
  </div>
  <div id="sla" class="panel">
    <h3>SLA 赔付批次审计台</h3>
    <table id="batches-table"><thead><tr><th>批次号</th><th>日期</th><th>商户</th><th>等级</th><th>币种</th><th>结算CNY</th><th>违约笔数</th><th>重复风险</th><th>赔付CNY</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody></table>
  </div>
</div>
<script>
const CSRF = "{csrf_tok}";
const POS = {{
  "risk-node-01": [50, 80],
  "settle-node-01": [50, 220],
  "risk-node-02": [320, 80],
  "risk-node-03": [320, 195],
  "settle-node-02": [320, 300],
  "pg-primary-01": [605, 165]
}};
let nodesCache = [], selectedNode = null;
function showTab(id, btn){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
function nodeColors(r){{
  if(r.status==='ISOLATED'||r.status==='DRAINED') return ['#fef3c7','#d97706'];
  if(r.status==='DEGRADED'||r.p99_ms>=1200) return ['#fee2e2','#dc2626'];
  if(r.p99_ms>=200) return ['#ffedd5','#ea580c'];
  return ['#dcfce7','#16a34a'];
}}
function renderSvg(){{
  const g = document.getElementById('svg-nodes');
  g.innerHTML = nodesCache.map(r=>{{
    const [x,y] = POS[r.node_name] || [40,40];
    const [bg,bd] = nodeColors(r);
    return `<g class="node-g" onclick="selectNode('${{r.node_name}}')">
      <rect x="${{x}}" y="${{y}}" width="175" height="72" rx="8" fill="${{bg}}" stroke="${{bd}}" stroke-width="2.5"/>
      <text x="${{x+10}}" y="${{y+22}}" font-size="13" font-weight="700" fill="#0f172a">${{r.node_name}}</text>
      <text x="${{x+10}}" y="${{y+41}}" font-size="11" fill="#334155">${{r.service}} [${{r.role}}]</text>
      <text x="${{x+10}}" y="${{y+59}}" font-size="11" font-weight="600" fill="${{bd}}">${{r.status}} | P99:${{r.p99_ms}}ms | Q:${{r.queue_depth}}</text>
    </g>`;
  }}).join('');
}}
function selectNode(name){{
  selectedNode = name;
  const r = nodesCache.find(x=>x.node_name===name);
  if(!r) return;
  document.getElementById('node-inspector').innerHTML = `
    <h4>${{r.node_name}}</h4>
    <div style="font-size:13px;line-height:1.8">
      <div>服务：<b>${{r.service}}</b> (${{r.role}})</div>
      <div>区域：${{r.zone}}</div>
      <div>当前状态：<span class="badge ${{r.status}}">${{r.status}}</span></div>
      <div>观测 P99：${{r.p99_ms}} ms | 队列/连接：${{r.queue_depth}}</div>
    </div>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="warn" onclick="setNode('${{r.node_name}}','ISOLATED')">隔离 (ISOLATE)</button>
      <button class="ok" onclick="setNode('${{r.node_name}}','ACTIVE')">恢复 (ACTIVE)</button>
    </div>`;
}}
async function setNode(name, status){{
  await fetch('/api/nodes/'+name, {{method:'PUT',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{status}})}});
  await loadAll();
  selectNode(name);
}}
async function loadAll(){{
  const n = await (await fetch('/api/nodes')).json();
  nodesCache = n.nodes;
  renderSvg();
  if(selectedNode) selectNode(selectedNode);

  const c = await (await fetch('/api/changes')).json();
  document.querySelector('#changes-table tbody').innerHTML = c.changes.map(r=>`<tr>
    <td>${{r.ticket_id}}</td><td>${{r.service}}</td><td><code>${{r.commit_sha.slice(0,10)}}</code></td><td>${{r.bottleneck_span}}</td>
    <td>${{r.scheduled_window_utc8}}</td><td><span class="badge ${{r.canary_status}}">${{r.canary_status}}</span></td>
    <td style="font-size:11px">${{r.replay_summary||'-'}}</td>
    <td><span class="badge ${{r.locked?'LOCKED':r.status}}">${{r.status}}${{r.locked?' (LOCKED)':''}}</span></td>
    <td style="white-space:nowrap">
      <button class="warn" ${{r.locked?'disabled':''}} onclick="runReplay('${{r.ticket_id}}')">触发灰度回放</button>
      <button class="ok" ${{(r.locked||r.canary_status!=='CANARY_PASSED')?'disabled':''}} onclick="approveChange('${{r.ticket_id}}')">签发排期</button>
    </td></tr>`).join('');

  const b = await (await fetch('/api/batches')).json();
  document.querySelector('#batches-table tbody').innerHTML = b.batches.map(r=>`<tr>
    <td>${{r.batch_id}}</td><td>${{r.batch_date}}</td><td>${{r.merchant_id}}</td><td>${{r.tier}}</td><td>${{r.currency}}</td>
    <td><input size="10" id="bs-${{r.batch_id}}" value="${{r.settled_amount_cny??''}}" ${{r.locked?'disabled':''}}></td>
    <td><input size="4" id="bb-${{r.batch_id}}" value="${{r.breached_tx_count??''}}" ${{r.locked?'disabled':''}}></td>
    <td><input size="4" id="bd-${{r.batch_id}}" value="${{r.duplicate_risk_count??''}}" ${{r.locked?'disabled':''}}></td>
    <td><input size="8" id="bp-${{r.batch_id}}" value="${{r.penalty_cny??''}}" ${{r.locked?'disabled':''}}></td>
    <td><span class="badge ${{r.audit_status}}">${{r.audit_status}}</span></td>
    <td><button ${{r.locked?'disabled':''}} onclick="saveBatch('${{r.batch_id}}')">审计确认</button></td></tr>`).join('');
}}
async function createChange(){{
  const body = {{
    service: document.getElementById('chg-svc').value,
    commit_sha: document.getElementById('chg-sha').value,
    bottleneck_span: document.getElementById('chg-span').value,
    scheduled_window_utc8: document.getElementById('chg-win').value,
    description: document.getElementById('chg-desc').value
  }};
  const r = await fetch('/api/changes',{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify(body)}});
  if(!r.ok) alert(await r.text());
  loadAll();
}}
async function runReplay(id){{
  const r = await fetch('/api/changes/'+id+'/canary-replay',{{method:'POST',headers:{{'X-CSRF-Token':CSRF}}}});
  if(!r.ok) alert(await r.text());
  loadAll();
}}
async function approveChange(id){{
  const r = await fetch('/api/changes/'+id+'/approve',{{method:'POST',headers:{{'X-CSRF-Token':CSRF}}}});
  if(!r.ok) alert(await r.text());
  loadAll();
}}
async function saveBatch(id){{
  const body = {{
    settled_amount_cny: parseFloat(document.getElementById('bs-'+id).value),
    breached_tx_count: parseInt(document.getElementById('bb-'+id).value),
    duplicate_risk_count: parseInt(document.getElementById('bd-'+id).value),
    penalty_cny: parseFloat(document.getElementById('bp-'+id).value),
    audit_status: 'AUDITED'
  }};
  const r = await fetch('/api/batches/'+id,{{method:'PUT',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify(body)}});
  if(!r.ok) alert(await r.text());
  loadAll();
}}
loadAll();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    init_db(reset=False)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORTAL_PORT", "8080")))
