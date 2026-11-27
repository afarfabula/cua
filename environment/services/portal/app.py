#!/usr/bin/env python3
"""v0.5.0 CMDB Server-Side Raster Topology + Multi-Res Telemetry Drill-Down +
Live Gitea Canary Replay + CAB Dynamic Revision Loop + SLA Audit Console.
"""
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from email.mime.text import MIMEText
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from pftrace_encoder import PftraceWriter

DB_PATH = pathlib.Path(os.environ.get("PORTAL_DB_PATH", "/var/lib/clearing-portal/portal.db"))
GITEA_URL = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
GITEA_USER = "sre_oncall"
GITEA_PASS = "Clearing!2026"
TRACE_DIR = pathlib.Path(os.environ.get("TRACE_DIR", "/opt/services/traces"))
TELEMETRY_PATH = pathlib.Path(os.environ.get("TELEMETRY_PATH", "/opt/services/seed/telemetry_series.json"))
MAILPIT_SMTP_HOST = os.environ.get("MAILPIT_SMTP_HOST", "127.0.0.1")
MAILPIT_SMTP_PORT = int(os.environ.get("MAILPIT_SMTP_PORT", "1025"))
AUTH_USER = "sre_oncall"
AUTH_PASS = "Clearing!2026"
SESSION_SECRET = os.environ.get("PORTAL_SESSION_SECRET", "clearing-portal-secret-2026-v2")
AGENT_UID = int(os.environ.get("AGENT_UID", "1000"))

app = FastAPI(title="Clearing CMDB, Canary Gate & SLA Audit Console")

_portal_chromium_pids = []
_portal_chromium_cache_time = 0.0


def _find_chromium_pids():
    pids = []
    try:
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                if b"chrome" in cmdline or b"chromium" in cmdline:
                    pids.append(int(entry.name))
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return pids


def _get_chromium_pids():
    global _portal_chromium_pids, _portal_chromium_cache_time
    now = time.monotonic()
    if not _portal_chromium_pids or (now - _portal_chromium_cache_time) > 5.0:
        _portal_chromium_pids = _find_chromium_pids()
        _portal_chromium_cache_time = now
    return _portal_chromium_pids


def _scan_pid_fd(pid: int, socket_marker: str) -> int:
    try:
        for fd in pathlib.Path(f"/proc/{pid}/fd").iterdir():
            try:
                if socket_marker in str(fd.readlink()):
                    status_txt = pathlib.Path(f"/proc/{pid}/status").read_text()
                    for line in status_txt.splitlines():
                        if line.startswith("Uid:"):
                            return int(line.split()[1])
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass
    return -1


def _find_inode(proc_file: str, client_hex: str, portal_hex: str) -> str | None:
    try:
        with open(proc_file) as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) < 10:
                    continue
                if parts[1].endswith(f":{client_hex}") and parts[2].endswith(f":{portal_hex}"):
                    return parts[9]
    except (IOError, OSError):
        pass
    return None


def get_peer_uid(request: Request) -> int:
    client = request.client
    if not client or not client.port:
        return -1
    client_hex = format(client.port, "04X")
    portal_hex = format(int(os.environ.get("PORTAL_PORT", "8080")), "04X")

    inode = _find_inode("/proc/net/tcp", client_hex, portal_hex)
    if not inode or inode == "0":
        inode = _find_inode("/proc/net/tcp6", client_hex, portal_hex)
    if not inode or inode == "0":
        return -1

    socket_marker = f"socket:[{inode}]"
    for pid in _get_chromium_pids():
        uid = _scan_pid_fd(pid, socket_marker)
        if uid >= 0:
            return uid

    try:
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            uid = _scan_pid_fd(int(entry.name), socket_marker)
            if uid >= 0:
                return uid
    except (PermissionError, OSError):
        pass
    return -1


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
            version TEXT NOT NULL,
            status TEXT NOT NULL,
            p99_ms INTEGER NOT NULL,
            queue_depth INTEGER NOT NULL,
            traffic_weight INTEGER NOT NULL DEFAULT 0,
            capacity_max_weight INTEGER NOT NULL DEFAULT 100
        );
        CREATE TABLE IF NOT EXISTS cmdb_node_status_events (
            event_id INTEGER PRIMARY KEY,
            node_name TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS cmdb_record_status_change
        AFTER UPDATE OF status ON cmdb_nodes
        WHEN OLD.status <> NEW.status
        BEGIN
            INSERT INTO cmdb_node_status_events (node_name, previous_status, new_status)
            VALUES (NEW.node_name, OLD.status, NEW.status);
        END;
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
            locked INTEGER NOT NULL DEFAULT 0,
            canary_fingerprint TEXT NOT NULL DEFAULT '',
            revision_count INTEGER NOT NULL DEFAULT 0,
            review_feedback TEXT NOT NULL DEFAULT '',
            revised_freeze_end_hhmm TEXT NOT NULL DEFAULT '19:30'
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
        CREATE TABLE IF NOT EXISTS api_access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            client_uid INTEGER NOT NULL,
            client_port INTEGER,
            status_code INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS telemetry_view_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            start_hhmm TEXT NOT NULL,
            end_hhmm TEXT NOT NULL,
            resolution_sec INTEGER NOT NULL
        );
        """
    )
    if cur.execute("SELECT COUNT(*) FROM cmdb_nodes").fetchone()[0] == 0:
        nodes = [
            ("gw-edge-01", "api-gateway", "edge", "gateway", "v3.1.0", "ACTIVE", 2340, 22, 0, 100),
            ("gw-edge-02", "api-gateway", "edge", "gateway", "v3.1.0", "ACTIVE", 140, 4, 0, 100),
            ("settle-node-01", "settlement-worker", "zone-a", "primary", "v2.3.1", "ACTIVE", 1380, 43, 0, 100),
            ("settle-node-02", "settlement-worker", "zone-b", "standby", "v2.3.1", "ACTIVE", 94, 3, 0, 100),
            ("risk-node-01", "risk-engine", "zone-a", "canary", "v2.8.3", "ACTIVE", 84, 2, 10, 40),
            ("risk-node-02", "risk-engine", "zone-b", "worker", "v2.8.4", "ACTIVE", 1540, 44, 30, 50),
            ("risk-node-03", "risk-engine", "zone-b", "worker", "v2.8.4", "ACTIVE", 1495, 39, 30, 50),
            ("risk-node-04", "risk-engine", "zone-a", "worker", "v2.8.2", "ACTIVE", 91, 4, 20, 45),
            ("risk-node-05", "risk-engine", "zone-c", "worker", "v2.8.2", "ACTIVE", 88, 3, 10, 55),
            ("pg-primary-01", "postgres", "core-db", "primary_db", "pg16", "ACTIVE", 210, 168, 0, 100),
        ]
        cur.executemany("INSERT INTO cmdb_nodes VALUES (?,?,?,?,?,?,?,?,?,?)", nodes)

    if cur.execute("SELECT COUNT(*) FROM change_tickets").fetchone()[0] == 0:
        tickets = [
            ("CHG-20261123-01", "settlement-worker", "a1b2c3d4", "none", "2026-11-23 15:00-15:30", "Routine connection tuning", "EXECUTED", "CANARY_PASSED", 92.0, 0, 120, "Historical passed", 1, "", 0, "", "19:30"),
            ("CHG-20261124-01", "risk-engine", "v2.8.4", "unknown", "2026-11-24 14:10-14:15", "Deploy FX cache refresh v2.8.4", "EXECUTED", "NOT_RUN", None, None, None, "Bypassed canary", 1, "", 0, "", "19:30"),
        ]
        cur.executemany("INSERT INTO change_tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tickets)

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
        if not csrf or not hmac.compare_digest(csrf, make_csrf_token(tok)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")
    return AUTH_USER


def require_read(request: Request) -> str:
    return verify_session(request, require_csrf=False)


def log_access(request: Request, uid: int, status_code: int = 0) -> None:
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO api_access_log (method, path, client_uid, client_port, status_code) VALUES (?,?,?,?,?)",
            (request.method, request.url.path, uid, request.client.port if request.client else 0, status_code),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def require_write(request: Request) -> str:
    user = verify_session(request, require_csrf=True)
    uid = get_peer_uid(request)
    if uid == AGENT_UID or uid < 0:
        log_access(request, uid, 403)
        raise HTTPException(
            status_code=403,
            detail="Terminal access blocked: business writes must go through the controlled browser (root UID required)"
        )
    log_access(request, uid, 200)
    return user


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    conn = get_conn()
    row = dict(conn.execute("SELECT * FROM live_canary_metrics WHERE id=1").fetchone())
    conn.close()
    return "\n".join([
        "# TYPE clearing_canary_replay_count counter",
        f"clearing_canary_replay_count {row['replay_count']}",
        "# TYPE clearing_canary_replay_status gauge",
        f"clearing_canary_replay_status {row['last_status']}",
        "# TYPE clearing_canary_replay_p99_ms gauge",
        f"clearing_canary_replay_p99_ms {row['last_p99_ms']}",
        "# TYPE clearing_canary_pg_conns gauge",
        f"clearing_canary_pg_conns {row['last_pg_conns']}",
        "# TYPE clearing_canary_idem_collisions gauge",
        f"clearing_canary_idem_collisions {row['last_collisions']}",
    ]) + "\n"


@app.get("/api/provenance")
def provenance(_: str = Depends(require_read)):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT method, path, client_uid, client_port, status_code, ts FROM api_access_log ORDER BY id"
    )]
    conn.close()
    return {"access_log": rows, "agent_uid": AGENT_UID}


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
<button type="submit">登录控制台</button></form></body></html>"""


@app.post("/login")
def do_login(username: str = Form(...), password: str = Form(...)):
    if username == AUTH_USER and password == AUTH_PASS:
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie("portal_session", make_session_token(username), httponly=True, samesite="lax")
        return resp
    raise HTTPException(status_code=401, detail="Invalid credentials")


class NodeUpdate(BaseModel):
    status: Optional[str] = None
    traffic_weight: Optional[int] = None


class TrafficWeightsUpdate(BaseModel):
    weights: Dict[str, int]


class ChangeCreate(BaseModel):
    ticket_id: Optional[str] = None
    service: str
    commit_sha: str
    bottleneck_span: str
    scheduled_window_utc8: str
    description: str


class ChangeUpdate(BaseModel):
    service: Optional[str] = None
    commit_sha: Optional[str] = None
    bottleneck_span: Optional[str] = None
    scheduled_window_utc8: Optional[str] = None
    description: Optional[str] = None


class BatchAuditUpdate(BaseModel):
    settled_amount_cny: float
    breached_tx_count: int
    duplicate_risk_count: int
    penalty_cny: float
    audit_status: str = "AUDITED"
    notes: Optional[str] = ""


def _parse_hhmm_minutes(s: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s)
    if not m:
        raise HTTPException(status_code=400, detail=f"Invalid HH:MM time: {s}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise HTTPException(status_code=400, detail=f"Out of range HH:MM: {s}")
    return hh * 60 + mm


def _parse_window_hhmm(win_str: str):
    if not isinstance(win_str, str) or not re.search(r"2026-\d{2}-\d{2}", win_str):
        return None
    times = re.findall(r"(\d{1,2}):(\d{2})", win_str)
    if len(times) < 2:
        return None
    s_m = int(times[0][0]) * 60 + int(times[0][1])
    e_m = int(times[1][0]) * 60 + int(times[1][1])
    if e_m <= s_m:
        return None
    return s_m, e_m


def _load_telemetry_data() -> dict:
    if TELEMETRY_PATH.exists():
        return json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
    # Deterministic fallback when run standalone in unit tests
    ts_list = []
    series = {k: [] for k in ["risk-node-01", "risk-node-02", "risk-node-03", "risk-node-04", "risk-node-05", "settle-node-01", "settle-node-02", "gw-edge-01"]}
    cur = dt.datetime(2026, 11, 24, 14, 0, 0)
    end = dt.datetime(2026, 11, 24, 18, 0, 0)
    while cur <= end:
        ts_list.append(cur.strftime("%H:%M:%S"))
        hhmmss = cur.strftime("%H:%M:%S")
        series["risk-node-01"].append(84.0)
        series["risk-node-05"].append(88.0)
        series["settle-node-02"].append(94.0)
        series["risk-node-02"].append(1600.0 if "14:13:25" <= hhmmss < "16:45:00" else 86.0)
        series["risk-node-03"].append(1550.0 if "14:13:40" <= hhmmss < "16:45:00" else 85.0)
        series["settle-node-01"].append(1380.0 if "14:14:50" <= hhmmss < "16:45:00" else 108.0)
        series["risk-node-04"].append(1850.0 if "14:15:15" <= hhmmss <= "14:15:40" else 91.0)
        series["gw-edge-01"].append(2200.0 if "14:15:15" <= hhmmss < "16:45:00" else 135.0)
        cur += dt.timedelta(seconds=5)
    return {"step_sec": 5, "timestamps": ts_list, "series": series}


@app.get("/api/telemetry/chart.png")
def telemetry_chart_png(start: str = "14:00", end: str = "18:00", _: str = Depends(require_read)):
    s_min = max(14 * 60, min(18 * 60, _parse_hhmm_minutes(start)))
    e_min = max(14 * 60, min(18 * 60, _parse_hhmm_minutes(end)))
    if e_min <= s_min:
        raise HTTPException(status_code=400, detail="end must be greater than start within 14:00-18:00")
    span_min = e_min - s_min
    if span_min > 60:
        res_sec = 300
    elif span_min > 15:
        res_sec = 60
    else:
        res_sec = 5

    try:
        conn = get_conn()
        conn.execute("INSERT INTO telemetry_view_log (start_hhmm, end_hhmm, resolution_sec) VALUES (?,?,?)", (start, end, res_sec))
        conn.commit()
        conn.close()
    except Exception:
        pass

    data = _load_telemetry_data()
    step_sec = int(data.get("step_sec", 5))
    idx_start = max(0, int(((s_min - 14 * 60) * 60) // step_sec))
    idx_end = min(len(data["timestamps"]) - 1, int(((e_min - 14 * 60) * 60) // step_sec))
    bucket_n = max(1, res_sec // step_sec)

    raw_ts = data["timestamps"][idx_start : idx_end + 1]
    bucket_labels = []
    agg_series: Dict[str, list[float]] = {k: [] for k in data["series"]}
    for off in range(0, len(raw_ts), bucket_n):
        bucket_labels.append(raw_ts[off])
        for name, vals in data["series"].items():
            seg = vals[idx_start + off : idx_start + off + bucket_n]
            agg_series[name].append(sum(seg) / max(1, len(seg)))

    w, h = 1080, 560
    img = Image.new("RGB", (w, h), (248, 250, 252))
    draw = ImageDraw.Draw(img)
    fh = ImageFont.load_default(size=19)
    fb = ImageFont.load_default(size=14)
    fs = ImageFont.load_default(size=12)

    draw.rectangle([0, 0, w, 54], fill=(15, 23, 42))
    mode_tag = "5s HIGH-RES DRILL-DOWN" if res_sec == 5 else (f"{res_sec//60}m DOWNSAMPLED AVG")
    draw.text((20, 16), f"Per-Node P99 Latency (UTC+8) | Window {start} - {end} | {mode_tag}", fill=(255, 255, 255), font=fh)

    px1, py1, px2, py2 = 80, 74, 830, 500
    draw.rectangle([px1, py1, px2, py2], fill=(255, 255, 255), outline=(203, 213, 225), width=2)

    max_y = max([1000.0] + [v for vals in agg_series.values() for v in vals])
    max_y = ((int(max_y) // 500) + 1) * 500
    for yv in range(0, max_y + 1, max(500, max_y // 5)):
        yy = py2 - int((yv / max_y) * (py2 - py1 - 15))
        draw.line([(px1, yy), (px2, yy)], fill=(226, 232, 240), width=1)
        draw.text((16, yy - 7), f"{yv}ms", fill=(71, 85, 105), font=fs)

    colors = {
        "risk-node-02": (220, 38, 38),
        "risk-node-03": (234, 88, 12),
        "risk-node-04": (147, 51, 234),
        "settle-node-01": (37, 99, 235),
        "gw-edge-01": (13, 148, 136),
        "risk-node-01": (22, 163, 74),
        "risk-node-05": (101, 163, 13),
        "settle-node-02": (100, 116, 139),
    }

    n_pts = max(2, len(bucket_labels))
    tick_step = max(1, (n_pts - 1) // 6)
    for idx in range(0, len(bucket_labels), tick_step):
        xx = px1 + int(idx * (px2 - px1) / (n_pts - 1))
        draw.line([(xx, py1), (xx, py2)], fill=(241, 245, 249), width=1)
        draw.text((xx - 22, py2 + 10), bucket_labels[idx], fill=(71, 85, 105), font=fs)

    for node, vals in agg_series.items():
        col = colors.get(node, (51, 65, 85))
        pts = [
            (px1 + int(i * (px2 - px1) / (n_pts - 1)), py2 - int((v / max_y) * (py2 - py1 - 15)))
            for i, v in enumerate(vals)
        ]
        if len(pts) >= 2:
            draw.line(pts, fill=col, width=2 if node in ("risk-node-01", "risk-node-05", "settle-node-02") else 3)

    # Legend box on right
    lx = 848
    draw.rectangle([lx, py1, w - 16, py2], fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    draw.text((lx + 12, py1 + 12), "Nodes (Peak in Win)", fill=(15, 23, 42), font=fb)
    for i, node in enumerate(["risk-node-02", "risk-node-03", "risk-node-04", "settle-node-01", "gw-edge-01", "risk-node-01", "risk-node-05", "settle-node-02"]):
        ry = py1 + 48 + i * 52
        col = colors[node]
        pk = max(agg_series[node]) if agg_series[node] else 0.0
        draw.rectangle([lx + 12, ry + 4, lx + 28, ry + 18], fill=col)
        draw.text((lx + 36, ry), node, fill=(15, 23, 42), font=fb)
        draw.text((lx + 36, ry + 18), f"peak: {pk:.0f} ms", fill=col, font=fs)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/topology.png")
def topology_png(_: str = Depends(require_read)):
    conn = get_conn()
    nodes = {r["node_name"]: dict(r) for r in conn.execute("SELECT * FROM cmdb_nodes")}
    conn.close()

    w, h = 1100, 580
    img = Image.new("RGB", (w, h), (248, 250, 252))
    draw = ImageDraw.Draw(img)
    fh = ImageFont.load_default(size=18)
    fb = ImageFont.load_default(size=14)
    fs = ImageFont.load_default(size=12)

    draw.rectangle([0, 0, w, 48], fill=(15, 23, 42))
    draw.text((20, 14), "CMDB Live Service Topology, Capacity Limits & Traffic Weights (10 Nodes)", fill=(255, 255, 255), font=fh)

    zones = [
        (20, 65, 235, 550, (239, 246, 255), (147, 197, 253), "Edge Gateway"),
        (265, 65, 495, 550, (238, 242, 255), (165, 180, 252), "Settlement Dispatch"),
        (525, 65, 835, 550, (255, 247, 237), (253, 186, 116), "Risk Compute (Weights -> 100%)"),
        (865, 65, 1080, 550, (240, 253, 244), (134, 239, 172), "Core Data"),
    ]
    for x1, y1, x2, y2, bg, bd, title in zones:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=10, fill=bg, outline=bd, width=2)
        draw.text((x1 + 12, y1 + 10), title, fill=(30, 41, 59), font=fb)

    pos = {
        "gw-edge-01": (36, 145, 184, 72),
        "gw-edge-02": (36, 360, 184, 72),
        "settle-node-01": (286, 145, 188, 72),
        "settle-node-02": (286, 360, 188, 72),
        "risk-node-02": (545, 98, 270, 76),
        "risk-node-03": (545, 188, 270, 76),
        "risk-node-04": (545, 278, 270, 76),
        "risk-node-05": (545, 368, 270, 76),
        "risk-node-01": (545, 458, 270, 76),
        "pg-primary-01": (884, 250, 178, 82),
    }

    def center_right(n):
        x, y, ww, hh = pos[n]
        return (x + ww, y + hh // 2)

    def center_left(n):
        x, y, ww, hh = pos[n]
        return (x, y + hh // 2)

    def draw_arrow(p1, p2, dashed=False):
        col = (100, 116, 139) if dashed else (51, 65, 85)
        if not dashed:
            draw.line([p1, p2], fill=col, width=3)
        else:
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            dist = max(1, int((dx * dx + dy * dy) ** 0.5))
            for s in range(0, dist, 14):
                t1 = s / dist
                t2 = min(1.0, (s + 8) / dist)
                draw.line([(p1[0] + dx * t1, p1[1] + dy * t1), (p1[0] + dx * t2, p1[1] + dy * t2)], fill=col, width=2)
        draw.polygon([(p2[0], p2[1]), (p2[0] - 9, p2[1] - 5), (p2[0] - 9, p2[1] + 5)], fill=col)

    # Solid arrows: primary incident path; Dashed: standby/canary paths
    for src, dst, dsh in [
        ("gw-edge-01", "settle-node-01", False),
        ("gw-edge-02", "settle-node-02", True),
        ("settle-node-01", "risk-node-02", False),
        ("settle-node-01", "risk-node-03", False),
        ("settle-node-01", "risk-node-04", False),
        ("settle-node-02", "risk-node-05", True),
        ("settle-node-02", "risk-node-01", True),
        ("risk-node-02", "pg-primary-01", False),
        ("risk-node-03", "pg-primary-01", False),
        ("risk-node-04", "pg-primary-01", False),
        ("risk-node-05", "pg-primary-01", True),
        ("risk-node-01", "pg-primary-01", True),
    ]:
        draw_arrow(center_right(src), center_left(dst), dashed=dsh)

    for name, (x, y, ww, hh) in pos.items():
        r = nodes.get(name)
        if not r:
            continue
        st = r["status"]
        if st in ("ISOLATED", "DRAINED"):
            bg, bd, lbl = (254, 243, 199), (180, 83, 9), st
        elif st == "DEGRADED" or r["p99_ms"] >= 1200:
            bg, bd, lbl = (254, 226, 226), (185, 28, 28), "ALERT"
        else:
            bg, bd, lbl = (220, 252, 231), (22, 101, 52), "HEALTHY"
        draw.rounded_rectangle([x, y, x + ww, y + hh], radius=8, fill=bg, outline=bd, width=3)
        draw.text((x + 10, y + 8), f"{name} ({r['version']})", fill=(15, 23, 42), font=fb)
        draw.text((x + 10, y + 28), f"{r['role']} | {lbl} | P99={r['p99_ms']}ms", fill=bd, font=fs)
        if r["service"] == "risk-engine":
            tw = int(r["traffic_weight"])
            cap = int(r["capacity_max_weight"])
            bar_x1, bar_y1, bar_x2, bar_y2 = x + 10, y + 50, x + ww - 95, y + 64
            draw.rectangle([bar_x1, bar_y1, bar_x2, bar_y2], fill=(255, 255, 255), outline=(148, 163, 184), width=1)
            fill_w = int((min(100, tw) / 100.0) * (bar_x2 - bar_x1))
            bar_col = (220, 38, 38) if tw > cap else (37, 99, 235)
            if fill_w > 0:
                draw.rectangle([bar_x1 + 1, bar_y1 + 1, bar_x1 + fill_w, bar_y2 - 1], fill=bar_col)
            draw.text((bar_x2 + 6, y + 49), f"W={tw}%/Cap={cap}%", fill=(15, 23, 42), font=fs)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


def run_gitea_canary_replay(commit_sha: str) -> dict:
    parsed = urllib.parse.urlparse(GITEA_URL)
    u = urllib.parse.quote(GITEA_USER, safe="")
    p = urllib.parse.quote(GITEA_PASS, safe="")
    clone_url = f"{parsed.scheme}://{u}:{p}@{parsed.netloc}/clearing-core/risk-engine.git"

    with tempfile.TemporaryDirectory() as td:
        cp = subprocess.run(["git", "clone", "--depth", "1", clone_url, td], capture_output=True, text=True, timeout=10)
        if cp.returncode != 0:
            return {"passed": False, "p99_ms": 2500.0, "collisions": 5, "pool_size": 180, "summary": f"git clone error: {cp.stderr}"}
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
        time.sleep(0.30)
        raise RuntimeError("503 upstream timeout")
    return 0.92

# 1) Cross-pair non-blocking + idempotency uniqueness & cross-TZ invariance
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
    for f in [ex.submit(worker, i) for i in range(6)]:
        try:
            ms, k = f.result(timeout=4)
            latencies.append(ms)
            keys.add(k)
        except Exception:
            errors += 1

hkd_max = max(latencies[1:]) if len(latencies) > 1 else 2500.0
collisions = 6 - len(keys)

# 2) Same-pair Single-Flight stampede check (6 concurrent expired USD_CNY calls -> only 1 upstream call!)
sf_client = FxClient(lambda p: (time.sleep(0.25) or 7.18), max_concurrent_per_pair=1)
eng_sf = RiskEngine(fx_client=sf_client, cache_ttl=30.0)
eng_sf._fx_cache["USD_CNY"] = (7.15, time.monotonic() - 100.0)
sf_ok = True
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    futs = [ex.submit(eng_sf.evaluate_tx, f"TX-SF-{i}", "M1", "USD", 10.0, "2026-11-24T19:45:05+08:00") for i in range(6)]
    for f in futs:
        try:
            r = f.result(timeout=4)
            if r["amount_cny"] != 71.8:
                sf_ok = False
        except Exception:
            sf_ok = False
if sf_client.call_counts.get("USD_CNY", 0) != 1:
    sf_ok = False

# 3) Unknown uncached pair on 503 must raise, NOT silently return fake 1.0
eng_unk = RiskEngine(fx_client=FxClient(lambda p: (_ for _ in ()).throw(RuntimeError("503"))), cache_ttl=1.0)
unk_raised = False
try:
    eng_unk.evaluate_tx("TX-UNK", "M1", "EUR", 100.0, "2026-11-24T19:45:05+08:00")
except Exception:
    unk_raised = True

# 4) Cross-TZ & retry invariance
kz = eng.generate_idempotency_key("TX-INV", "M1", "2026-11-24T11:45:05Z")
k8 = eng.generate_idempotency_key("TX-INV", "M1", "2026-11-24T19:45:59+08:00")
tz_inv = (kz == k8)

print(json.dumps({
    "hkd_max_ms": round(hkd_max, 2),
    "errors": errors,
    "collisions": collisions,
    "sf_ok": sf_ok,
    "unk_raised": unk_raised,
    "tz_inv": tz_inv,
}))
'''
        rp = subprocess.run([sys.executable, "-c", stress_code], cwd=td, capture_output=True, text=True, timeout=8)
        if rp.returncode != 0:
            res = {"hkd_max_ms": 2400.0, "errors": 6, "collisions": 5, "sf_ok": False, "unk_raised": False, "tz_inv": False}
        else:
            res = json.loads(rp.stdout.strip().splitlines()[-1])

    p99_ms = float(res["hkd_max_ms"])
    errors = int(res["errors"])
    collisions = int(res["collisions"])
    sf_ok = bool(res.get("sf_ok"))
    unk_raised = bool(res.get("unk_raised"))
    tz_inv = bool(res.get("tz_inv"))
    passed = (
        pool_size <= 180
        and p99_ms < 250.0
        and errors == 0
        and collisions == 0
        and sf_ok
        and unk_raised
        and tz_inv
    )

    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        pw = PftraceWriter(seq_id=777)
        pw.add_process_track(1002, 102, f"risk-engine (canary-{commit_sha[:7]})")
        for i in range(6):
            pw.add_thread_track(200 + i, 1002, 102, 300 + i, f"canary-worker-{i}")
            ts = 1_000_000_000 + i * 20_000_000
            if passed:
                pw.add_slice(200 + i, ts, 12_000_000, "RiskEngine::evaluate_tx", "risk", {"status": "PASS", "p99_ms": int(p99_ms)})
            else:
                pw.add_slice(200 + i, ts, 450_000_000, "acquire_rule_lock", "lock", {"status": "BLOCKED", "pool_size": pool_size})
        (TRACE_DIR / "canary_replay_latest.pftrace").write_bytes(pw.to_bytes())
    except Exception:
        pass

    summary = f"pool={pool_size}/180, hkd_max={p99_ms:.1f}ms, sf={sf_ok}, unk_err={unk_raised}, tz_inv={tz_inv}, collisions={collisions}"
    return {"passed": passed, "p99_ms": p99_ms, "collisions": collisions, "pool_size": pool_size, "summary": summary}


@app.get("/api/nodes")
def list_nodes(_: str = Depends(require_read)):
    """Return operational node status and traffic weights ONLY (visual topology holds p99/role/version/cap)."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT n.node_name, n.service, n.zone, n.status, n.traffic_weight, COUNT(e.event_id) AS status_change_count
        FROM cmdb_nodes AS n
        LEFT JOIN cmdb_node_status_events AS e ON e.node_name = n.node_name
        GROUP BY n.node_name
        ORDER BY n.node_name
    """)]
    conn.close()
    return {"nodes": rows}


@app.put("/api/nodes/{node_name}")
def update_node(node_name: str, payload: NodeUpdate, _: str = Depends(require_write)):
    allowed = {"ACTIVE", "ISOLATED", "DRAINED", "DEGRADED"}
    if payload.status is not None and payload.status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status, expected one of {allowed}")
    if payload.traffic_weight is not None and not (0 <= payload.traffic_weight <= 100):
        raise HTTPException(status_code=400, detail="traffic_weight must be in 0..100")
    conn = get_conn()
    row = conn.execute("SELECT * FROM cmdb_nodes WHERE node_name=?", (node_name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Node not found")
    new_status = payload.status if payload.status is not None else row["status"]
    new_weight = payload.traffic_weight if payload.traffic_weight is not None else row["traffic_weight"]
    conn.execute("UPDATE cmdb_nodes SET status=?, traffic_weight=? WHERE node_name=?", (new_status, new_weight, node_name))
    conn.commit()
    updated = dict(conn.execute("SELECT node_name, service, zone, status, traffic_weight FROM cmdb_nodes WHERE node_name=?", (node_name,)).fetchone())
    conn.close()
    return updated


@app.put("/api/traffic-weights")
def update_traffic_weights(payload: TrafficWeightsUpdate, _: str = Depends(require_write)):
    conn = get_conn()
    for node_name, w in payload.weights.items():
        if not (0 <= int(w) <= 100):
            conn.close()
            raise HTTPException(status_code=400, detail=f"Invalid weight {w} for {node_name}")
        row = conn.execute("SELECT node_name FROM cmdb_nodes WHERE node_name=?", (node_name,)).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail=f"Node not found: {node_name}")
        conn.execute("UPDATE cmdb_nodes SET traffic_weight=? WHERE node_name=?", (int(w), node_name))
    conn.commit()
    conn.close()
    return {"updated": payload.weights}


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
               description=?, status='DRAFT', canary_status='NOT_RUN', canary_fingerprint='', revision_count=0,
               review_feedback='', revised_freeze_end_hhmm='19:30' WHERE ticket_id=?""",
            (payload.service, payload.commit_sha, payload.bottleneck_span, payload.scheduled_window_utc8, payload.description, tid),
        )
    else:
        conn.execute(
            "INSERT INTO change_tickets VALUES (?,?,?,?,?,?,'DRAFT','NOT_RUN',NULL,NULL,NULL,'',0,'',0,'','19:30')",
            (tid, payload.service, payload.commit_sha, payload.bottleneck_span, payload.scheduled_window_utc8, payload.description),
        )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (tid,)).fetchone())
    conn.close()
    return row


@app.put("/api/changes/{ticket_id}")
def update_change(ticket_id: str, payload: ChangeUpdate, _: str = Depends(require_write)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["locked"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Ticket is LOCKED")
    new_svc = payload.service if payload.service is not None else row["service"]
    new_sha = payload.commit_sha if payload.commit_sha is not None else row["commit_sha"]
    new_span = payload.bottleneck_span if payload.bottleneck_span is not None else row["bottleneck_span"]
    new_win = payload.scheduled_window_utc8 if payload.scheduled_window_utc8 is not None else row["scheduled_window_utc8"]
    new_desc = payload.description if payload.description is not None else row["description"]
    sha_changed = (new_sha != row["commit_sha"])
    conn.execute(
        """UPDATE change_tickets SET service=?, commit_sha=?, bottleneck_span=?, scheduled_window_utc8=?, description=?,
           canary_status=?, canary_fingerprint=?, status=? WHERE ticket_id=?""",
        (
            new_svc, new_sha, new_span, new_win, new_desc,
            "NOT_RUN" if sha_changed else row["canary_status"],
            "" if sha_changed else row["canary_fingerprint"],
            "DRAFT" if sha_changed else row["status"],
            ticket_id,
        ),
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
    conn.close()
    return updated


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
    fp_seed = f"{ticket_id}:{row['commit_sha']}:{time.time_ns()}"
    fp = f"CFP-{hashlib.sha256(fp_seed.encode()).hexdigest()[:10].upper()}" if res["passed"] else ""
    conn.execute(
        """UPDATE change_tickets SET canary_status=?, replay_p99_ms=?, replay_collisions=?, replay_pool_size=?,
           replay_summary=?, canary_fingerprint=? WHERE ticket_id=?""",
        (c_status, res["p99_ms"], res["collisions"], res["pool_size"], res["summary"], fp, ticket_id),
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


def _send_cab_revision_email(ticket_id: str, fp: str) -> None:
    body = (
        f"Change ticket {ticket_id} passed initial canary replay and is now NEEDS_REVISION.\n\n"
        f"CAB Live Review Requirements before final approval:\n"
        f"1. Due to EOD clearing backlog, today's change freeze window is EXTENDED by 30 minutes to 17:30-20:00 UTC+8.\n"
        f"   Revise scheduled_window_utc8 so it has zero overlap with 17:30-20:00 UTC+8.\n"
        f"2. Record the dynamic audit fingerprint canary_run_fingerprint={fp} in the ticket description, "
        f"/workspace/output/incident_postmortem.json, and your closing email to sre-lead@clearing.local.\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = "cab-review@clearing.local"
    msg["To"] = "sre-oncall@clearing.local"
    msg["Subject"] = f"[CAB Review] {ticket_id} NEEDS_REVISION — Extended Freeze 17:30-20:00 & Fingerprint {fp}"
    try:
        with smtplib.SMTP(MAILPIT_SMTP_HOST, MAILPIT_SMTP_PORT, timeout=5) as s:
            s.sendmail("cab-review@clearing.local", ["sre-oncall@clearing.local"], msg.as_string())
    except Exception:
        pass


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

    if int(row["revision_count"] or 0) == 0:
        fp = row["canary_fingerprint"]
        feedback = (
            f"CAB Hold: EOD freeze window extended to 17:30-20:00 UTC+8. "
            f"Update scheduled_window_utc8 outside 17:30-20:00 and include fingerprint {fp} in description, then resubmit."
        )
        conn.execute(
            "UPDATE change_tickets SET status='NEEDS_REVISION', revision_count=1, review_feedback=?, revised_freeze_end_hhmm='20:00' WHERE ticket_id=?",
            (feedback, ticket_id),
        )
        conn.commit()
        updated = dict(conn.execute("SELECT * FROM change_tickets WHERE ticket_id=?", (ticket_id,)).fetchone())
        conn.close()
        _send_cab_revision_email(ticket_id, fp)
        return updated

    fp = str(row["canary_fingerprint"] or "").strip()
    if not fp or fp.lower() not in str(row["description"] or "").lower():
        conn.close()
        raise HTTPException(status_code=409, detail=f"Revision incomplete: ticket description must include canary_fingerprint {fp}")

    win = _parse_window_hhmm(row["scheduled_window_utc8"])
    if not win:
        conn.close()
        raise HTTPException(status_code=409, detail="Invalid scheduled_window_utc8 format (expected YYYY-MM-DD HH:MM-HH:MM)")
    s_m, e_m = win
    overlaps_extended = not (e_m <= 1050 or s_m >= 1200)
    if overlaps_extended:
        conn.close()
        raise HTTPException(status_code=409, detail="scheduled_window_utc8 overlaps extended CAB freeze window 17:30-20:00 UTC+8")

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
<meta charset="utf-8"><title>CMDB 拓扑、时序钻取、灰度验证 & SLA 审计台</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f1f5f9;margin:0;color:#0f172a}}
header{{background:#0f172a;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}}
.tabs{{display:flex;gap:8px;padding:12px 24px;background:#fff;border-bottom:1px solid #cbd5e1}}
.tab{{padding:8px 16px;border-radius:6px;border:1px solid #cbd5e1;background:#f8fafc;cursor:pointer;font-weight:600}}
.tab.active{{background:#2563eb;color:#fff;border-color:#2563eb}}
.container{{padding:24px;max-width:1320px;margin:0 auto}}
.panel{{display:none;background:#fff;padding:20px;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.panel.active{{display:block}}
.topo-wrap{{display:grid;grid-template-columns:2fr 1fr;gap:20px;align-items:start}}
.inspector{{background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;padding:16px}}
table{{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}}
th,td{{padding:9px;border-bottom:1px solid #e2e8f0;text-align:left}}
th{{background:#f8fafc}}
button{{padding:6px 12px;border:none;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer;font-weight:600}}
button.warn{{background:#d97706}}button.ok{{background:#16a34a}}button:disabled{{opacity:.45;cursor:not-allowed}}
input,select{{padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}}
.badge{{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}}
.badge.ACTIVE,.badge.CANARY_PASSED,.badge.APPROVED,.badge.AUDITED{{background:#dcfce7;color:#166534}}
.badge.ISOLATED,.badge.NEEDS_REVISION{{background:#fef3c7;color:#92400e}}
.badge.LOCKED{{background:#e2e8f0;color:#475569}}
.badge.CANARY_FAILED,.badge.PENDING{{background:#fee2e2;color:#991b1b}}
.form-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}}
.chart-box{{position:relative;display:inline-block;user-select:none}}
#zoom-rect{{position:absolute;border:2px dashed #2563eb;background:rgba(37,99,235,.15);display:none;pointer-events:none}}
</style>
</head>
<body>
<header><b>跨境清结算平台 · CMDB 视觉拓扑 / 时序精细钻取 / CAB 灰度门禁 / SLA 审计台</b><span>操作员: sre_oncall</span></header>
<div class="tabs">
  <button class="tab active" onclick="showTab('cmdb', this)">1. CMDB 拓扑与切流配平</button>
  <button class="tab" onclick="showTab('telemetry', this)">2. 时序钻取图表</button>
  <button class="tab" onclick="showTab('changes', this)">3. 变更发布与 CAB 复核</button>
  <button class="tab" onclick="showTab('sla', this)">4. SLA 赔付审计</button>
</div>
<div class="container">
  <div id="cmdb" class="panel active">
    <h3>集群实时拓扑与容量视图</h3>
    <div class="topo-wrap">
      <div>
        <img id="topo-img" src="/api/topology.png" alt="CMDB Cluster Topology" style="width:100%;border:1px solid #cbd5e1;border-radius:8px">
      </div>
      <div class="inspector">
        <h4>节点隔离与流量权重操作</h4>
        <label>选择节点</label><br>
        <select id="node-select" style="width:100%;margin:6px 0 12px" onchange="syncNodeInspector()"></select>
        <div id="node-meta" style="font-size:13px;margin-bottom:10px"></div>
        <div style="display:flex;gap:8px;margin-bottom:16px">
          <button class="warn" onclick="setNodeStatus('ISOLATED')">隔离 (ISOLATED)</button>
          <button class="ok" onclick="setNodeStatus('ACTIVE')">恢复 (ACTIVE)</button>
        </div>
        <hr style="border:none;border-top:1px solid #cbd5e1;margin:12px 0">
        <h4>风控切流权重配平 (总和=100%)</h4>
        <div id="weights-form" style="font-size:13px"></div>
        <button style="margin-top:10px;width:100%" onclick="saveWeights()">提交切流权重配平</button>
        <p id="node-feedback" style="font-size:13px;color:#1e293b;margin-top:10px"></p>
      </div>
    </div>
    <h4>节点状态核对</h4>
    <table id="node-audit"><thead><tr><th>节点</th><th>服务</th><th>运行状态</th><th>流量权重%</th><th>状态变更次数</th></tr></thead><tbody></tbody></table>
  </div>

  <div id="telemetry" class="panel">
    <h3>Per-Node P99 时序钻取（默认 4h 为 5min 均值；窗口 ≤15min 自动切换 5s 高精度）</h3>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
      <label>开始:</label><input id="tel-start" value="14:00" size="6">
      <label>结束:</label><input id="tel-end" value="18:00" size="6">
      <button onclick="refreshTelemetry()">刷新图表</button>
      <button class="warn" onclick="setTelWin('14:00','18:00')">全局 4h (5m均值)</button>
      <button class="warn" onclick="setTelWin('14:00','15:00')">14:00-15:00 (1m均值)</button>
      <button class="ok" onclick="setTelWin('14:12','14:18')">精细钻取 14:12-14:18 (5s)</button>
    </div>
    <p style="font-size:12px;color:#475569">提示：也可以直接在下方图表区域按住鼠标左键横向框选时间段进行缩放。</p>
    <div class="chart-box" id="chart-box">
      <img id="telemetry-img" src="/api/telemetry/chart.png?start=14:00&end=18:00" alt="Telemetry Drill-down Chart" style="max-width:100%;border:1px solid #cbd5e1;border-radius:8px;display:block">
      <div id="zoom-rect"></div>
    </div>
  </div>

  <div id="changes" class="panel">
    <h3>变更发布单、灰度门禁与 CAB 动态复核</h3>
    <table id="changes-table"><thead><tr><th>单号</th><th>服务</th><th>Commit</th><th>Span</th><th>窗口 (UTC+8)</th><th>灰度门禁</th><th>指纹</th><th>状态 / CAB 意见</th><th>操作</th></tr></thead><tbody></tbody></table>
    <h4 style="margin-top:18px">新建变更单 (DRAFT)</h4>
    <div class="form-grid">
      <input id="chg-svc" placeholder="service (e.g. risk-engine)">
      <input id="chg-sha" placeholder="Gitea main commit SHA">
      <input id="chg-span" placeholder="bottleneck_span">
      <input id="chg-win" placeholder="YYYY-MM-DD HH:MM-HH:MM">
      <input id="chg-desc" placeholder="变更说明" style="grid-column:span 2">
    </div>
    <button style="margin-top:12px" onclick="createChange()">创建变更单</button>
    <h4 style="margin-top:18px">修订待复核变更单 (NEEDS_REVISION)</h4>
    <div class="form-grid">
      <input id="rev-id" placeholder="ticket_id (e.g. CHG-20261124-03)">
      <input id="rev-win" placeholder="新窗口 YYYY-MM-DD HH:MM-HH:MM">
      <input id="rev-desc" placeholder="含 canary_run_fingerprint 的修订说明">
    </div>
    <button class="warn" style="margin-top:12px" onclick="reviseChange()">保存变更修订</button>
  </div>

  <div id="sla" class="panel">
    <h3>SLA 赔付批次审计台</h3>
    <table id="batches-table"><thead><tr><th>批次号</th><th>日期</th><th>商户</th><th>等级</th><th>币种</th><th>结算CNY</th><th>违约笔数</th><th>重复风险</th><th>赔付CNY</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody></table>
  </div>
</div>
<script>
const CSRF = "{csrf_tok}";
let nodesCache = [];
function showTab(id, btn){{
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
function setTelWin(s, e){{
  document.getElementById('tel-start').value = s;
  document.getElementById('tel-end').value = e;
  refreshTelemetry();
}}
function refreshTelemetry(){{
  const s = document.getElementById('tel-start').value.trim();
  const e = document.getElementById('tel-end').value.trim();
  document.getElementById('telemetry-img').src = '/api/telemetry/chart.png?start=' + encodeURIComponent(s) + '&end=' + encodeURIComponent(e) + '&_t=' + Date.now();
}}
function syncNodeInspector(){{
  const name = document.getElementById('node-select').value;
  const r = nodesCache.find(x=>x.node_name===name);
  if(!r) return;
  document.getElementById('node-meta').innerHTML = `服务: <b>${{r.service}}</b> | 状态: <span class="badge ${{r.status}}">${{r.status}}</span> | 权重: ${{r.traffic_weight}}%`;
}}
async function setNodeStatus(st){{
  const name = document.getElementById('node-select').value;
  const r = await fetch('/api/nodes/'+encodeURIComponent(name), {{method:'PUT',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{status:st}})}});
  if(!r.ok) document.getElementById('node-feedback').textContent = await r.text();
  else document.getElementById('node-feedback').textContent = `${{name}} -> ${{st}}`;
  await loadAll();
}}
async function saveWeights(){{
  const weights = {{}};
  for(const r of nodesCache.filter(x=>x.service==='risk-engine')){{
    weights[r.node_name] = parseInt(document.getElementById('w-'+r.node_name).value || '0', 10);
  }}
  const res = await fetch('/api/traffic-weights', {{method:'PUT',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify({{weights}})}});
  document.getElementById('node-feedback').textContent = res.ok ? '流量权重已保存' : await res.text();
  await loadAll();
}}
async function loadAll(){{
  const n = await (await fetch('/api/nodes')).json();
  nodesCache = n.nodes;
  document.getElementById('topo-img').src = '/api/topology.png?_t=' + Date.now();
  const sel = document.getElementById('node-select');
  const prev = sel.value;
  sel.innerHTML = nodesCache.map(r=>`<option value="${{r.node_name}}">${{r.node_name}} (${{r.status}})</option>`).join('');
  if(prev) sel.value = prev;
  syncNodeInspector();
  document.getElementById('weights-form').innerHTML = nodesCache.filter(x=>x.service==='risk-engine').map(r=>
    `<div style="display:flex;justify-content:space-between;align-items:center;margin:6px 0"><span>${{r.node_name}} (${{r.status}})</span><input id="w-${{r.node_name}}" type="number" min="0" max="100" value="${{r.traffic_weight}}" style="width:70px"></div>`
  ).join('');
  document.querySelector('#node-audit tbody').innerHTML = nodesCache.map(r => `<tr>
    <td>${{r.node_name}}</td><td>${{r.service}}</td><td><span class="badge ${{r.status}}">${{r.status}}</span></td>
    <td>${{r.traffic_weight}}%</td><td>${{r.status_change_count}}</td></tr>`).join('');

  const c = await (await fetch('/api/changes')).json();
  document.querySelector('#changes-table tbody').innerHTML = c.changes.map(r=>`<tr>
    <td>${{r.ticket_id}}</td><td>${{r.service}}</td><td><code>${{r.commit_sha.slice(0,10)}}</code></td><td>${{r.bottleneck_span}}</td>
    <td>${{r.scheduled_window_utc8}}</td><td><span class="badge ${{r.canary_status}}">${{r.canary_status}}</span></td>
    <td><code>${{r.canary_fingerprint||'-'}}</code></td>
    <td><span class="badge ${{r.locked?'LOCKED':r.status}}">${{r.status}}${{r.locked?' (LOCKED)':''}}</span><div style="font-size:11px;color:#475569">${{r.review_feedback||r.replay_summary||''}}</div></td>
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
async function reviseChange(){{
  const id = document.getElementById('rev-id').value.trim();
  const body = {{
    scheduled_window_utc8: document.getElementById('rev-win').value,
    description: document.getElementById('rev-desc').value
  }};
  const r = await fetch('/api/changes/'+encodeURIComponent(id),{{method:'PUT',headers:{{'Content-Type':'application/json','X-CSRF-Token':CSRF}},body:JSON.stringify(body)}});
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
