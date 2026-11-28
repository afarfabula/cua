import io
import json
import os
import pathlib
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from PIL import Image, ImageDraw
from pydantic import BaseModel
import uvicorn

from common import (
    AUTOBU_TEAMS,
    MAX_SIM_RUNS,
    PORTAL_PASS,
    PORTAL_USER,
    RACK_SPECS,
    TEAM_ORDER,
    build_sim_payload,
    call_sim_engine,
    compute_state_hash,
    get_conn,
    make_session_cookie,
    now_utc8,
    require_auth,
    verify_session_cookie,
)

app = FastAPI(title="Rack & Fabric Console", version="0.6.4")

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Login - Rack &amp; Fabric Console</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e293b;padding:28px;border-radius:12px;width:360px;border:1px solid #334155}
input,button{width:100%;padding:10px;margin:8px 0;border-radius:6px;box-sizing:border-box}
input{background:#0f172a;color:#f8fafc;border:1px solid #475569}
button{background:#2563eb;color:#fff;border:none;font-weight:600;cursor:pointer}</style></head>
<body><form class="card" method="post" action="/login">
<h2>Rack &amp; Fabric Console</h2><p style="color:#94a3b8;font-size:13px">H100 GPU 集群拓扑排布与 Staging 仿真平台</p>
<input name="username" placeholder="Username" value="cap_lead" required>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">登录控制台</button></form></body></html>"""


class AllocationItem(BaseModel):
    rack: str
    bay: int
    shift: str
    team: str
    rdma: bool = False
    priority_iops: bool = False


class AllocationsUpdate(BaseModel):
    assignments: List[AllocationItem]


def _sanitize_sim_for_client(raw: dict, runs_used: int, run_id: int, created_at: str) -> dict:
    bleed_total = sum(r.get("thermal_bleed_bays", 0) for r in raw.get("racks", []))
    io_any = bool(raw.get("io_contention"))
    racks_clean = []
    for r in raw.get("racks", []):
        racks_clean.append({
            "rack": r["rack"],
            "spine": r["spine"],
            "zone": r["zone"],
            "has_rdma": r["has_rdma"],
            "cap_kw": r["cap_kw"],
            "day_kw": r["day_kw"],
            "night_kw": r["night_kw"],
            "peak_kw": r["peak_kw"],
            "power_ok": r["power_ok"],
        })
    teams_clean = {}
    for tid, info in raw.get("teams", {}).items():
        eff_pct = round(info["coverage"] * info["efficiency"] * 100.0, 2)
        teams_clean[tid] = {
            "raw_bays": info["raw_bays"],
            "eff_bays": info["eff_bays"],
            "req_bays": info["req_bays"],
            "accepted_shift": info["accepted_shift"],
            "coverage_pct": round(info["coverage"] * 100.0, 2),
            "effective_throughput_pct": eff_pct,
            "effective_utility": round(info["utility"], 3),
        }
    advisories = []
    if bleed_total > 0 or io_any:
        advisories.append("Telemetry anomaly detected — inspect /api/simulations/heatmap.png and per-team effective throughput")
    if not advisories and raw.get("hard_feasible") and raw.get("u_total", 0.0) >= 99.0:
        advisories.append("Cluster telemetry nominal across thermal, fabric, and IO planes")
    return {
        "run_id": run_id,
        "created_at": created_at,
        "runs_used": runs_used,
        "runs_remaining": max(0, MAX_SIM_RUNS - runs_used),
        "max_runs": MAX_SIM_RUNS,
        "status": raw["status"],
        "hard_feasible": raw["hard_feasible"],
        "r0_intact": raw["r0_intact"],
        "power_ok": raw["power_ok"],
        "budget_ok": raw["budget_ok"],
        "monthly_cost_usd": raw["monthly_cost_usd"],
        "budget_cap_usd": raw["budget_cap_usd"],
        "effective_cluster_utility": raw["u_total"],
        "cost_efficiency_score": raw["cost_eff"],
        "advisories": advisories,
        "racks": racks_clean,
        "teams": teams_clean,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "rack-console"}


@app.get("/login")
def login_page():
    return HTMLResponse(LOGIN_HTML)


@app.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username != PORTAL_USER or password != PORTAL_PASS:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    cookie_val = make_session_cookie(username)
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        resp = JSONResponse({"ok": True, "user": username})
    else:
        resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie("cluster_session", cookie_val, httponly=True, samesite="lax")
    return resp


@app.get("/api/cluster")
def get_cluster(user: str = Depends(require_auth)):
    with get_conn() as conn:
        racks = [dict(r) for r in conn.execute("SELECT * FROM rack_specs ORDER BY rack")]
        assigns = [dict(r) for r in conn.execute("SELECT * FROM allocations ORDER BY rack, bay, shift")]
        runs_used = conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
    return {
        "racks": racks,
        "assignments": assigns,
        "simulations_used": runs_used,
        "simulations_remaining": max(0, MAX_SIM_RUNS - runs_used),
        "max_simulations": MAX_SIM_RUNS,
    }


@app.get("/api/allocations")
def get_allocations(user: str = Depends(require_auth)):
    with get_conn() as conn:
        assigns = [dict(r) for r in conn.execute("SELECT rack, bay, shift, team, rdma, priority_iops FROM allocations ORDER BY rack, bay, shift")]
    return {"assignments": assigns}


def _shift_mask(shift: str) -> int:
    return 3 if shift == "24H" else (1 if shift == "DAY_ONLY" else (2 if shift == "NIGHT_ONLY" else 0))


def _trigger_concurrent_bu_claims(conn, run_id: int, ts: str) -> List[dict]:
    rows = [dict(r) for r in conn.execute("SELECT rack, bay, shift, team FROM allocations")]
    occ = {}
    for r in rows:
        key = (r["rack"], int(r["bay"]))
        occ[key] = occ.get(key, 0) | _shift_mask(r["shift"])

    claimed = []

    def try_claim(rack: str, bay: int, shift: str, team: str) -> bool:
        m = _shift_mask(shift)
        if occ.get((rack, bay), 0) & m:
            return False
        occ[(rack, bay)] = occ.get((rack, bay), 0) | m
        conn.execute(
            "INSERT INTO allocations (rack, bay, shift, team, rdma, priority_iops) VALUES (?, ?, ?, ?, 0, 0)",
            (rack, bay, shift, team),
        )
        conn.execute(
            "INSERT INTO auto_bu_claims (run_id, rack, bay, shift, team, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, rack, bay, shift, team, ts),
        )
        claimed.append({"rack": rack, "bay": bay, "shift": shift, "team": team})
        return True

    def claim_from(candidates: List[tuple], team: str) -> None:
        for rack, bay, shift in candidates:
            if try_claim(rack, bay, shift, team):
                return
        for rk in ["R5", "R4", "R3", "R2", "R1"]:
            for b in [3, 1, 2, 0]:
                for sh in ["24H", "NIGHT_ONLY", "DAY_ONLY"]:
                    if try_claim(rk, b, sh, team):
                        return

    if run_id == 1:
        claim_from(
            [
                ("R5", 3, "24H"), ("R3", 3, "24H"), ("R4", 3, "24H"),
                ("R5", 1, "24H"), ("R3", 1, "24H"),
                ("R5", 3, "NIGHT_ONLY"), ("R3", 3, "NIGHT_ONLY"),
                ("R5", 1, "NIGHT_ONLY"), ("R3", 1, "NIGHT_ONLY"),
                ("R4", 3, "DAY_ONLY"), ("R5", 3, "DAY_ONLY"),
            ],
            "SHARED-AUTO-BU-MED",
        )
    elif run_id == 2:
        claim_from(
            [
                ("R5", 1, "NIGHT_ONLY"), ("R3", 1, "NIGHT_ONLY"), ("R3", 3, "NIGHT_ONLY"),
                ("R4", 1, "NIGHT_ONLY"), ("R4", 3, "NIGHT_ONLY"),
                ("R5", 1, "DAY_ONLY"), ("R3", 1, "DAY_ONLY"),
                ("R5", 1, "24H"), ("R3", 1, "24H"),
            ],
            "SHARED-AUTO-BU-MED",
        )
    elif run_id == 3:
        claim_from(
            [
                ("R4", 1, "24H"), ("R5", 1, "24H"),
                ("R4", 1, "DAY_ONLY"), ("R5", 1, "DAY_ONLY"),
                ("R3", 1, "24H"), ("R2", 1, "24H"), ("R1", 1, "24H"),
            ],
            "SHARED-AUTO-BU-HIGH",
        )
    return claimed


@app.post("/api/allocations")
def save_allocations(body: AllocationsUpdate, user: str = Depends(require_auth)):
    valid_racks = {r["rack"] for r in RACK_SPECS}
    valid_teams = {"PROD-ONLINE-INFERENCE", *TEAM_ORDER, *AUTOBU_TEAMS}
    valid_shifts = {"24H", "DAY_ONLY", "NIGHT_ONLY"}
    seen_slots = {}
    for item in body.assignments:
        if item.rack not in valid_racks:
            raise HTTPException(status_code=400, detail=f"Unknown rack: {item.rack}")
        if not (0 <= item.bay <= 3):
            raise HTTPException(status_code=400, detail=f"Invalid bay index: {item.bay}")
        if item.shift not in valid_shifts:
            raise HTTPException(status_code=400, detail=f"Invalid shift: {item.shift}")
        if item.team not in valid_teams:
            raise HTTPException(status_code=400, detail=f"Unknown team: {item.team}")
        mask = _shift_mask(item.shift)
        key = (item.rack, item.bay)
        if seen_slots.get(key, 0) & mask:
            raise HTTPException(status_code=400, detail=f"Overlapping shift on {item.rack} Bay {item.bay}")
        seen_slots[key] = seen_slots.get(key, 0) | mask
        if item.rdma and item.rack not in {"R4", "R5"}:
            raise HTTPException(status_code=400, detail=f"RDMA fabric hardware only installed on Spine-B racks R4/R5 (got {item.rack})")

    with get_conn() as conn:
        locked_claims = sorted(
            (r["rack"], int(r["bay"]), r["shift"], r["team"])
            for r in conn.execute("SELECT rack, bay, shift, team FROM auto_bu_claims")
        )
        submitted_autobu = sorted(
            (x.rack, int(x.bay), x.shift, x.team)
            for x in body.assignments
            if x.team in AUTOBU_TEAMS
        )
        for req in locked_claims:
            if req not in submitted_autobu:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot modify or evict locked shared-pool reservation {req[0]} Bay {req[1]} ({req[2]}, {req[3]})",
                )
        for sub in submitted_autobu:
            if sub not in locked_claims:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot manually add synthetic shared-pool reservation {sub[3]}",
                )

        conn.execute("DELETE FROM allocations")
        for item in body.assignments:
            conn.execute(
                "INSERT INTO allocations (rack, bay, shift, team, rdma, priority_iops) VALUES (?, ?, ?, ?, ?, ?)",
                (item.rack, item.bay, item.shift, item.team, 1 if item.rdma else 0, 1 if item.priority_iops else 0),
            )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]
    return {"ok": True, "saved_assignments": count}


@app.post("/api/simulations/run")
def run_simulation(user: str = Depends(require_auth)):
    with get_conn() as conn:
        used = conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
        if used >= MAX_SIM_RUNS:
            raise HTTPException(
                status_code=429,
                detail=f"Staging simulation quota exhausted ({used}/{MAX_SIM_RUNS}). Commit or review existing telemetry.",
            )
        payload = build_sim_payload(conn)
        state_hash = compute_state_hash(payload)
        raw = call_sim_engine(payload)
        ts = now_utc8()
        cur = conn.execute(
            "INSERT INTO simulations (state_hash, status, hard_feasible, monthly_cost_usd, u_total, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state_hash, raw["status"], 1 if raw["hard_feasible"] else 0, raw["monthly_cost_usd"], raw["u_total"], json.dumps(raw), ts),
        )
        run_id = cur.lastrowid
        claimed = _trigger_concurrent_bu_claims(conn, run_id, ts)
        if claimed:
            payload = build_sim_payload(conn)
            state_hash = compute_state_hash(payload)
            raw = call_sim_engine(payload)
            conn.execute(
                "UPDATE simulations SET state_hash=?, status=?, hard_feasible=?, monthly_cost_usd=?, u_total=?, raw_json=? WHERE run_id=?",
                (state_hash, raw["status"], 1 if raw["hard_feasible"] else 0, raw["monthly_cost_usd"], raw["u_total"], json.dumps(raw), run_id),
            )
        conn.commit()
        used_now = conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
    resp = _sanitize_sim_for_client(raw, used_now, run_id, ts)
    if claimed:
        slots_desc = ", ".join(f"{c['rack']} Bay {c['bay']} ({c['shift']})" for c in claimed)
        resp["advisories"].append(
            f"Shared pool event: idle slot(s) {slots_desc} were claimed by concurrent tenant workload; telemetry reflects updated cluster state."
        )
    return resp


@app.get("/api/simulations/latest")
def get_latest_simulation(user: str = Depends(require_auth)):
    with get_conn() as conn:
        used = conn.execute("SELECT COUNT(*) FROM simulations").fetchone()[0]
        row = conn.execute("SELECT * FROM simulations ORDER BY run_id DESC LIMIT 1").fetchone()
    if not row:
        return {"runs_used": 0, "runs_remaining": MAX_SIM_RUNS, "max_runs": MAX_SIM_RUNS, "latest": None}
    raw = json.loads(row["raw_json"])
    return _sanitize_sim_for_client(raw, used, row["run_id"], row["created_at"])


@app.get("/api/simulations/heatmap.png")
def get_heatmap_png(user: str = Depends(require_auth)):
    with get_conn() as conn:
        assigns = [dict(r) for r in conn.execute("SELECT * FROM allocations")]
        sim_row = conn.execute("SELECT * FROM simulations ORDER BY run_id DESC LIMIT 1").fetchone()
        cur_hash = compute_state_hash(build_sim_payload(conn))

    W, H = 1280, 660
    img = Image.new("RGB", (W, H), (15, 23, 42))
    draw = ImageDraw.Draw(img)

    if not sim_row:
        draw.rectangle([0, 0, W, 58], fill=(30, 41, 59))
        draw.text((24, 14), "H100 CLUSTER RACK ELEVATION, THERMAL BLEED & NVMe IO TELEMETRY", fill=(248, 250, 252))
        draw.text((24, 34), "No Staging Simulation Run Yet — Run a Staging Simulation to generate telemetry heatmap", fill=(251, 191, 36))
        draw.rectangle([40, 96, W - 40, H - 40], fill=(30, 41, 59), outline=(71, 85, 105), width=2)
        draw.text((340, 310), "Telemetry heatmap will render after your first Staging simulation run (POST /api/simulations/run).", fill=(148, 163, 184))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    raw = json.loads(sim_row["raw_json"])
    stale_note = f" | STALE PREVIEW (layout changed since Sim #{sim_row['run_id']})" if sim_row["state_hash"] != cur_hash else ""
    bleed_map = {}
    for bs in raw.get("_internal", {}).get("bleed_slots", []):
        bleed_map[(bs["rack"], int(bs["bay"]))] = bs
    rack_info = {r["rack"]: r for r in raw.get("racks", [])}

    # Title banner
    draw.rectangle([0, 0, W, 58], fill=(30, 41, 59))
    draw.text((24, 14), "H100 CLUSTER RACK ELEVATION, THERMAL BLEED & NVMe IO TELEMETRY", fill=(248, 250, 252))
    sim_label = f"Sim #{sim_row['run_id']} | Status: {raw['status']} | Utility: {raw['u_total']:.2f}/100 | Cost: ${raw['monthly_cost_usd']:,.0f}{stale_note}"
    draw.text((24, 34), sim_label, fill=(251, 191, 36) if stale_note else (148, 163, 184))

    # Legend on top right
    draw.rectangle([790, 10, 1260, 48], fill=(15, 23, 42), outline=(71, 85, 105))
    draw.rectangle([802, 20, 818, 36], fill=(239, 68, 68))
    draw.text((824, 21), "High-TDP", fill=(226, 232, 240))
    draw.rectangle([895, 20, 911, 36], fill=(59, 130, 246))
    draw.text((917, 21), "Med-TDP", fill=(226, 232, 240))
    draw.rectangle([985, 20, 1001, 36], fill=(34, 197, 94))
    draw.text((1007, 21), "Low-TDP", fill=(226, 232, 240))
    draw.rectangle([1072, 20, 1088, 36], fill=(249, 115, 22), outline=(254, 215, 170), width=2)
    draw.text((1094, 21), "Thermal Bleed", fill=(253, 186, 116))

    rack_w, rack_gap = 188, 18
    x0 = 32
    slot_lookup = {}
    for a in assigns:
        slot_lookup.setdefault((a["rack"], int(a["bay"])), []).append(a)

    for idx, rspec in enumerate(RACK_SPECS):
        rname = rspec["rack"]
        rx = x0 + idx * (rack_w + rack_gap)
        ry = 78
        rh = 540
        rstat = rack_info.get(rname, {})
        border_col = (245, 158, 11) if rspec["protected"] else ((239, 68, 68) if not rstat.get("power_ok", True) else (71, 85, 105))
        draw.rectangle([rx, ry, rx + rack_w, ry + rh], fill=(30, 41, 59), outline=border_col, width=2)

        hdr = f"{rname} ({rspec['spine']})"
        sub = f"{rspec['zone']} | {'RDMA' if rspec['has_rdma'] else 'Std'}"
        draw.text((rx + 10, ry + 10), hdr, fill=(248, 250, 252))
        draw.text((rx + 10, ry + 26), sub, fill=(148, 163, 184))

        # Power bar
        peak_kw = float(rstat.get("peak_kw", 0.0))
        cap_kw = float(rspec["cap_kw"])
        pct = min(1.0, peak_kw / cap_kw) if cap_kw > 0 else 0.0
        draw.rectangle([rx + 10, ry + 48, rx + rack_w - 10, ry + 60], fill=(15, 23, 42), outline=(51, 65, 85))
        pcol = (239, 68, 68) if peak_kw > cap_kw else ((234, 179, 8) if pct > 0.85 else (34, 197, 94))
        draw.rectangle([rx + 11, ry + 49, rx + 11 + int((rack_w - 22) * pct), ry + 59], fill=pcol)
        draw.text((rx + 10, ry + 64), f"Power: {peak_kw:.1f}/{cap_kw:.0f} kW", fill=(203, 213, 225))

        # IO lane indicator (visual bar only)
        io_cont = bool(rstat.get("io_contended", False))
        draw.rectangle([rx + 10, ry + 84, rx + rack_w - 10, ry + 98], fill=(127, 29, 29) if io_cont else (20, 83, 45))
        draw.text((rx + 14, ry + 85), "NVMe IO: SATURATED" if io_cont else "NVMe IO: NOMINAL", fill=(254, 226, 226) if io_cont else (187, 247, 208))

        # Visual asymmetric ceiling return-air hot-zone hatch on R2 and R4 above Bay 3
        if rname in {"R2", "R4"}:
            draw.rectangle([rx + 10, ry + 101, rx + rack_w - 10, ry + 110], fill=(127, 29, 29))
            for hx in range(rx + 12, rx + rack_w - 12, 12):
                draw.line([hx, ry + 110, hx + 6, ry + 101], fill=(248, 113, 113), width=2)

        # 4 vertical bays: Bay 3 at top down to Bay 0 at bottom
        bay_h = 88
        bay_gap = 12
        for vis_row, b in enumerate([3, 2, 1, 0]):
            by = ry + 112 + vis_row * (bay_h + bay_gap)
            entries = slot_lookup.get((rname, b), [])
            bleed = (rname, b) in bleed_map
            hot_top_zone = (rname in {"R2", "R4"} and b == 3)
            box_outline = (249, 115, 22) if bleed else ((185, 28, 28) if hot_top_zone else (51, 65, 85))
            draw.rectangle([rx + 10, by, rx + rack_w - 10, by + bay_h], fill=(15, 23, 42), outline=box_outline, width=3 if bleed else (2 if hot_top_zone else 1))
            draw.text((rx + 16, by + 5), f"Bay {b}", fill=(148, 163, 184))

            if bleed:
                # Visual thermal halo bar (no text formula spoiler)
                draw.rectangle([rx + 12, by + 2, rx + rack_w - 12, by + 16], fill=(194, 65, 12))
                for hx in range(rx + 16, rx + rack_w - 16, 14):
                    draw.rectangle([hx, by + 5, hx + 6, by + 13], fill=(253, 186, 116))

            if not entries:
                draw.text((rx + 56, by + 42), "[ IDLE 4xH100 ]", fill=(71, 85, 105))
            else:
                for e_idx, a in enumerate(entries[:2]):
                    tm = a["team"]
                    tdp_cls = "HIGH" if tm in {"T1", "T3", "T6", "SHARED-AUTO-BU-HIGH"} else ("MED" if tm in {"T2", "T4", "PROD-ONLINE-INFERENCE", "SHARED-AUTO-BU-MED"} else "LOW")
                    fill_c = (153, 27, 27) if tdp_cls == "HIGH" else ((30, 64, 175) if tdp_cls == "MED" else (22, 101, 52))
                    if tm == "PROD-ONLINE-INFERENCE":
                        fill_c = (120, 53, 15)
                    elif tm in AUTOBU_TEAMS:
                        fill_c = (107, 33, 168)
                    sub_y = by + 24 + e_idx * 30
                    sub_h = 56 if len(entries) == 1 else 27
                    draw.rectangle([rx + 16, sub_y, rx + rack_w - 16, sub_y + sub_h], fill=fill_c)
                    flags = []
                    if a.get("rdma"):
                        flags.append("RDMA")
                    if a.get("priority_iops"):
                        flags.append("IOPS")
                    fstr = ("+" + ",".join(flags)) if flags else ""
                    lbl = f"{tm[:8]} {a['shift'][:5]}{fstr}"
                    draw.text((rx + 20, sub_y + 6), lbl, fill=(248, 250, 252))

            # Draw red vertical thermal arrows between adjacent bleeding bays
            if b > 0 and (rname, b) in bleed_map and (rname, b - 1) in bleed_map:
                arrow_y = by + bay_h + 2
                draw.rectangle([rx + 78, arrow_y, rx + 110, arrow_y + 8], fill=(249, 115, 22))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/")
def index(request: Request):
    if not verify_session_cookie(request.cookies.get("cluster_session")):
        return RedirectResponse("/login", status_code=302)
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Rack &amp; Fabric Console (:8081)</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:18px}
.top{display:flex;justify-content:space-between;align-items:center;background:#1e293b;padding:14px 20px;border-radius:10px;margin-bottom:16px;border:1px solid #334155}
.btn{background:#2563eb;color:#fff;border:none;padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600}
.btn.warn{background:#d97706}.btn.ok{background:#16a34a}
.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}
.rack{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px}
.rack.prot{border-color:#f59e0b}
.bay{background:#0f172a;border:1px solid #475569;border-radius:6px;padding:8px;margin-top:8px;min-height:92px}
.chip{background:#334155;padding:4px 6px;border-radius:4px;margin-top:4px;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.chip.HIGH{background:#7f1d1d}.chip.MED{background:#1e3a8a}.chip.LOW{background:#14532d}.chip.PROD{background:#78350f}.chip.AUTOBU{background:#581c87;border:1px solid #c084fc}
.panel{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-top:16px}
select,input{background:#0f172a;color:#f8fafc;border:1px solid #475569;border-radius:4px;padding:4px 6px}
</style></head><body>
<div class="top">
  <div><h2 style="margin:0">Rack &amp; Fabric Console (:8081)</h2>
  <div id="quota" style="font-size:13px;color:#94a3b8">Loading...</div></div>
  <div style="display:flex;gap:10px;align-items:center">
    <a href="http://127.0.0.1:8082/" target="_blank" style="color:#93c5fd">Team Desk (:8082)</a>
    <a href="http://127.0.0.1:8083/" target="_blank" style="color:#93c5fd">FinOps &amp; Release (:8083)</a>
    <button class="btn ok" onclick="saveAll()">保存机柜排布</button>
    <button class="btn warn" onclick="runSim()">运行 Staging 仿真</button>
  </div>
</div>
<div class="panel" style="margin-bottom:16px;margin-top:0">
  <strong>快速分配槽位：</strong>
  机柜 <select id="fRack"><option>R1</option><option>R2</option><option>R3</option><option>R4</option><option>R5</option></select>
  Bay <select id="fBay"><option value="3">Bay 3 (Top)</option><option value="2">Bay 2</option><option value="1">Bay 1</option><option value="0">Bay 0 (Bottom)</option></select>
  班次 <select id="fShift"><option>24H</option><option>DAY_ONLY</option><option>NIGHT_ONLY</option></select>
  团队 <select id="fTeam"><option>T1</option><option>T2</option><option>T3</option><option>T4</option><option>T5</option><option>T6</option><option>T7</option><option>T8</option></select>
  <label><input type="checkbox" id="fRdma"> RDMA</label>
  <label><input type="checkbox" id="fIops"> Priority IOPS</label>
  <button class="btn" onclick="addAssign()">添加/覆盖</button>
</div>
<div id="racks" class="grid"></div>
<div class="panel">
  <h3>最新 Staging 仿真结果与热力图</h3>
  <pre id="simOut" style="background:#0f172a;padding:10px;border-radius:6px;overflow:auto;max-height:240px">暂无仿真记录</pre>
  <img id="hmImg" src="/api/simulations/heatmap.png" style="max-width:100%;border-radius:8px;margin-top:10px;border:1px solid #334155">
</div>
<script>
let state = {racks:[], assignments:[]};
async function load(){
  const r = await fetch('/api/cluster'); state = await r.json();
  document.getElementById('quota').textContent = `Staging 仿真配额已用: ${state.simulations_used} / ${state.max_simulations} (剩余 ${state.simulations_remaining})`;
  renderRacks();
  const s = await fetch('/api/simulations/latest'); const sd = await s.json();
  document.getElementById('simOut').textContent = JSON.stringify(sd, null, 2);
  document.getElementById('hmImg').src = '/api/simulations/heatmap.png?t=' + Date.now();
}
function tcls(t){if(t.startsWith('PROD'))return 'PROD';if(t.startsWith('SHARED-AUTO-BU'))return 'AUTOBU';if(['T1','T3','T6'].includes(t))return 'HIGH';if(['T2','T4'].includes(t))return 'MED';return 'LOW';}
function renderRacks(){
  const box = document.getElementById('racks'); box.innerHTML = '';
  for(const rk of state.racks){
    const div = document.createElement('div');
    div.className = 'rack' + (rk.protected ? ' prot' : '');
    div.innerHTML = `<div><strong>${rk.rack}</strong> (${rk.spine})<br><span style="font-size:12px;color:#94a3b8">${rk.zone} | ${rk.has_rdma?'RDMA':'Std'} | ${rk.cap_kw}kW</span></div>`;
    for(const b of [3,2,1,0]){
      const bd = document.createElement('div'); bd.className = 'bay';
      bd.innerHTML = `<div style="font-size:11px;color:#94a3b8">Bay ${b}</div>`;
      const items = state.assignments.filter(a => a.rack===rk.rack && a.bay===b);
      for(const a of items){
        const ch = document.createElement('div'); ch.className = 'chip ' + tcls(a.team);
        const locked = rk.protected || a.team.startsWith('SHARED-AUTO-BU');
        const del = locked ? '<span style="color:#e9d5ff;font-size:10px">LOCKED</span>' : `<button onclick="rmAssign('${a.rack}',${a.bay},'${a.shift}')" style="background:none;border:none;color:#fca5a5;cursor:pointer">x</button>`;
        ch.innerHTML = `<span>${a.team} (${a.shift})${a.rdma?'+RDMA':''}${a.priority_iops?'+IOPS':''}</span>${del}`;
        bd.appendChild(ch);
      }
      div.appendChild(bd);
    }
    box.appendChild(div);
  }
}
function rmAssign(rack, bay, shift){
  state.assignments = state.assignments.filter(a => !(a.rack===rack && a.bay===bay && a.shift===shift));
  renderRacks();
}
function addAssign(){
  const rack=fRack.value, bay=+fBay.value, shift=fShift.value, team=fTeam.value, rdma=fRdma.checked, priority_iops=fIops.checked;
  state.assignments = state.assignments.filter(a => !(a.rack===rack && a.bay===bay && (shift==='24H'||a.shift==='24H'||a.shift===shift)));
  state.assignments.push({rack,bay,shift,team,rdma,priority_iops});
  renderRacks();
}
async function saveAll(){
  const r = await fetch('/api/allocations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({assignments:state.assignments})});
  const d = await r.json(); if(!r.ok) alert(d.detail||JSON.stringify(d)); else await load();
}
async function runSim(){
  const r = await fetch('/api/simulations/run',{method:'POST'});
  const d = await r.json(); if(!r.ok) alert(d.detail||JSON.stringify(d)); else await load();
}
load();
</script></body></html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORTAL_PORT", "8081")))
