import base64
import hashlib
import hmac
import json
import os
import pathlib
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

DB_PATH = pathlib.Path(os.environ.get("CLUSTER_DB_PATH", "/var/lib/cluster-ops/cluster.db"))
SIM_ENGINE_BIN = os.environ.get("SIM_ENGINE_BIN", "/opt/services/engine/sim_engine")
SECRET_FILE = pathlib.Path(os.environ.get("CLUSTER_SECRET_FILE", "/var/lib/cluster-ops/.session_secret"))
PORTAL_USER = "cap_lead"
PORTAL_PASS = "Cluster!2026"
MAX_SIM_RUNS = 6

RACK_SPECS = [
    {"rack": "R0", "spine": "Spine-A", "zone": "Zone-A", "has_rdma": False, "cap_kw": 24.0, "protected": True},
    {"rack": "R1", "spine": "Spine-A", "zone": "Zone-A", "has_rdma": False, "cap_kw": 22.0, "protected": False},
    {"rack": "R2", "spine": "Spine-A", "zone": "Zone-A", "has_rdma": False, "cap_kw": 22.0, "protected": False},
    {"rack": "R3", "spine": "Spine-A", "zone": "Zone-B", "has_rdma": False, "cap_kw": 20.0, "protected": False},
    {"rack": "R4", "spine": "Spine-B", "zone": "Zone-B", "has_rdma": True, "cap_kw": 22.0, "protected": False},
    {"rack": "R5", "spine": "Spine-B", "zone": "Zone-B", "has_rdma": True, "cap_kw": 24.0, "protected": False},
]

TEAM_ORDER = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]


def now_utc8() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_session_secret() -> str:
    env_sec = os.environ.get("CLUSTER_SESSION_SECRET")
    if env_sec:
        return env_sec
    if SECRET_FILE.is_file():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    return "cluster-ops-dev-secret-v060"


def make_session_cookie(username: str = PORTAL_USER) -> str:
    sig = hmac.new(get_session_secret().encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{username}:{sig}"


def verify_session_cookie(val: Optional[str]) -> Optional[str]:
    if not val or ":" not in val:
        return None
    user, sig = val.split(":", 1)
    if user != PORTAL_USER:
        return None
    expected = make_session_cookie(user).split(":", 1)[1]
    if hmac.compare_digest(sig, expected):
        return user
    return None


def require_auth(request: Request) -> str:
    cookie = request.cookies.get("cluster_session")
    user = verify_session_cookie(cookie)
    if user:
        return user
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
            u, p = decoded.split(":", 1)
            if u == PORTAL_USER and p == PORTAL_PASS:
                return u
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="Authentication required at /login")


def get_conn(db_path: pathlib.Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rack_specs (
            rack TEXT PRIMARY KEY,
            spine TEXT NOT NULL,
            zone TEXT NOT NULL,
            has_rdma INTEGER NOT NULL,
            cap_kw REAL NOT NULL,
            protected INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rack TEXT NOT NULL,
            bay INTEGER NOT NULL,
            shift TEXT NOT NULL,
            team TEXT NOT NULL,
            rdma INTEGER NOT NULL DEFAULT 0,
            priority_iops INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS team_tickets (
            team_id TEXT PRIMARY KEY,
            team_name TEXT NOT NULL,
            tdp_class TEXT NOT NULL,
            tdp_kw REAL NOT NULL,
            workload_profile TEXT NOT NULL,
            weight REAL NOT NULL,
            initial_req_bays INTEGER NOT NULL,
            req_bays INTEGER NOT NULL,
            accepted_shift TEXT NOT NULL,
            require_rdma INTEGER NOT NULL DEFAULT 0,
            require_single_spine INTEGER NOT NULL DEFAULT 0,
            require_no_bleed INTEGER NOT NULL DEFAULT 0,
            require_priority_iops INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            feedback TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS negotiation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS simulations (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            hard_feasible INTEGER NOT NULL,
            monthly_cost_usd REAL NOT NULL,
            u_total REAL NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finops_po (
            team_id TEXT PRIMARY KEY,
            po_code TEXT NOT NULL,
            po_status TEXT NOT NULL,
            approved_budget_usd REAL NOT NULL,
            legacy_ref TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS release_commits (
            commit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_code TEXT NOT NULL,
            total_monthly_cost_usd REAL NOT NULL,
            u_total REAL NOT NULL,
            allocations_json TEXT NOT NULL,
            tickets_json TEXT NOT NULL,
            sim_json TEXT NOT NULL,
            committed_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def build_sim_payload(conn: sqlite3.Connection) -> Dict[str, Any]:
    assigns = []
    for row in conn.execute("SELECT rack, bay, shift, team, rdma, priority_iops FROM allocations ORDER BY rack, bay, shift"):
        assigns.append({
            "rack": row["rack"],
            "bay": int(row["bay"]),
            "shift": row["shift"],
            "team": row["team"],
            "rdma": bool(row["rdma"]),
            "priority_iops": bool(row["priority_iops"]),
        })
    negotiated = {}
    for row in conn.execute("SELECT * FROM team_tickets"):
        negotiated[row["team_id"]] = {
            "req_bays": int(row["req_bays"]),
            "shift": row["accepted_shift"],
            "require_rdma": bool(row["require_rdma"]),
            "require_single_spine": bool(row["require_single_spine"]),
            "require_no_bleed": bool(row["require_no_bleed"]),
            "require_priority_iops": bool(row["require_priority_iops"]),
        }
    return {"assignments": assigns, "negotiated": negotiated}


def call_sim_engine(payload: Dict[str, Any], engine_bin: str = SIM_ENGINE_BIN) -> Dict[str, Any]:
    raw_in = json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run([engine_bin], input=raw_in, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"sim_engine failed rc={proc.returncode}: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout)


def compute_tariff_cost(allocations: List[Dict[str, Any]]) -> Dict[str, Any]:
    rack_map = {r["rack"]: r for r in RACK_SPECS}
    by_team: Dict[str, Dict[str, Any]] = {}
    total = 0.0
    for a in allocations:
        rack = a["rack"]
        team = a["team"]
        if rack == "R0" or team == "PROD-ONLINE-INFERENCE":
            continue
        shift = a["shift"]
        base = 6200.0 if shift == "24H" else (3800.0 if shift == "DAY_ONLY" else 2400.0)
        rdma_fee = 400.0 if (a.get("rdma") and rack_map.get(rack, {}).get("has_rdma")) else 0.0
        egress = 4000.0 if (team == "T2" and rack_map.get(rack, {}).get("zone") != "Zone-A") else 0.0
        line_cost = base + rdma_fee + egress
        total += line_cost
        entry = by_team.setdefault(team, {"team_id": team, "bays": 0, "shift_cost_usd": 0.0, "rdma_fee_usd": 0.0, "cross_zone_egress_usd": 0.0, "total_cost_usd": 0.0})
        entry["bays"] += 1
        entry["shift_cost_usd"] += base
        entry["rdma_fee_usd"] += rdma_fee
        entry["cross_zone_egress_usd"] += egress
        entry["total_cost_usd"] += line_cost
    return {"total_monthly_cost_usd": round(total, 2), "budget_cap_usd": 138000.0, "by_team": by_team}
