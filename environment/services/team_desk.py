import json
import os
import re
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

from common import (
    PORTAL_PASS,
    PORTAL_USER,
    RACK_SPECS,
    build_sim_payload,
    compute_state_hash,
    get_conn,
    make_session_cookie,
    now_utc8,
    require_auth,
    verify_session_cookie,
)

app = FastAPI(title="Team Demand & Negotiation Desk", version="0.6.0")

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Login - Team Negotiation Desk</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e293b;padding:28px;border-radius:12px;width:360px;border:1px solid #334155}
input,button{width:100%;padding:10px;margin:8px 0;border-radius:6px;box-sizing:border-box}
input{background:#0f172a;color:#f8fafc;border:1px solid #475569}
button{background:#2563eb;color:#fff;border:none;font-weight:600;cursor:pointer}</style></head>
<body><form class="card" method="post" action="/login">
<h2>Team Negotiation Desk</h2><p style="color:#94a3b8;font-size:13px">多团队算力需求协商与 SLA 签收台</p>
<input name="username" placeholder="Username" value="cap_lead" required>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">登录协商台</button></form></body></html>"""

LEGACY_ARCHIVE = [
    {
        "archive_id": "REQ-2026-089",
        "team_id": "T8",
        "team_name": "Legacy-OCR-Batch",
        "status": "ALREADY_FULFILLED_ON_L40_POOL",
        "cluster": "legacy-cluster-02 (16x L40S)",
        "approved_at": "2026-11-18T11:20:00+08:00",
        "notes": "Duplicate H100 expansion request filed by mistake; workload already scheduled and running on legacy-cluster-02.",
    },
    {
        "archive_id": "REQ-2026-074",
        "team_id": "T2",
        "team_name": "Realtime-Search-Rank",
        "status": "HISTORICAL_INCIDENT_NOTE",
        "cluster": "Zone-A",
        "approved_at": "2026-10-29T16:40:00+08:00",
        "notes": "Search ranking checkpoint flush saturated shared rack NVMe bus when co-located with offline batch eval jobs on the same rack.",
    },
]


class NegotiateRequest(BaseModel):
    message: str = ""
    offered_gpus: Optional[int] = None
    offered_bays: Optional[int] = None
    shift: Optional[str] = None
    rdma: Optional[bool] = None
    priority_iops: Optional[bool] = None
    single_spine: Optional[bool] = None
    no_thermal_bleed: Optional[bool] = None


class DeferRequest(BaseModel):
    reason: str = ""


def _parse_intent(body: NegotiateRequest) -> dict:
    txt = (body.message or "").lower()
    bays = body.offered_bays
    if bays is None and body.offered_gpus is not None:
        bays = body.offered_gpus // 4
    if bays is None:
        m_gpu = re.search(r"(\d+)\s*(?:卡|gpu|gpus)", txt)
        m_bay = re.search(r"(\d+)\s*(?:bay|bays|槽)", txt)
        if m_gpu:
            bays = int(m_gpu.group(1)) // 4
        elif m_bay:
            bays = int(m_bay.group(1))

    shift = body.shift
    if not shift:
        if any(k in txt for k in ("night", "夜间", "错峰夜", "offpeak", "off-peak", "21:00")):
            shift = "NIGHT_ONLY"
        elif any(k in txt for k in ("day_only", "day-only", "day shift", "白天", "日间", "09:00", "daytime", "day peak", "day-peak")):
            shift = "DAY_ONLY"
        elif "24h" in txt or "全天" in txt:
            shift = "24H"

    rdma = body.rdma if body.rdma is not None else any(k in txt for k in ("rdma", "spine-b", "专网", "高速互联", "roce"))
    iops = body.priority_iops if body.priority_iops is not None else any(k in txt for k in ("iops", "priority", "优先io", "nvme qos", "qos"))
    single_spine = body.single_spine if body.single_spine is not None else any(k in txt for k in ("single spine", "single-spine", "同spine", "单spine", "不跨交换", "同交换"))
    no_bleed = body.no_thermal_bleed if body.no_thermal_bleed is not None else any(k in txt for k in ("no bleed", "thermal", "降频", "散热", "交错", "不相邻", "零热"))
    return {
        "bays": bays,
        "shift": shift,
        "rdma": bool(rdma),
        "iops": bool(iops),
        "single_spine": bool(single_spine),
        "no_bleed": bool(no_bleed),
    }


def verify_team_signoff(team_id: str, ticket: dict, sim_raw: dict, conn) -> tuple[bool, str]:
    assigns = [dict(r) for r in conn.execute("SELECT * FROM allocations WHERE team=?", (team_id,))]
    rack_map = {r["rack"]: r for r in RACK_SPECS}
    tinfo = sim_raw.get("teams", {}).get(team_id, {})

    if team_id in {"T7", "T8"}:
        return False, f"{team_id} is ineligible for production H100 allocation; use defer after checking FinOps / Legacy Archive."

    if tinfo.get("coverage", 0.0) < 0.999:
        return False, (
            f"{team_id} demands {ticket['req_bays']} effective bays on shift {ticket['accepted_shift']} "
            f"(currently coverage={tinfo.get('coverage', 0.0)*100:.1f}%)."
        )

    if team_id == "T1":
        if ticket["require_rdma"] and not tinfo.get("all_rdma_ok"):
            return False, "T1 concession requires all bays placed on Spine-B (R4/R5) with RDMA enabled."
        if tinfo.get("cross_spine"):
            return False, "T1 rejected cross-Spine layout; AllReduce bandwidth collapsed across Spine-A/Spine-B."

    elif team_id == "T2":
        if not tinfo.get("zone_a_ok"):
            return False, "T2 requires all bays in Zone-A (R1/R2) for local NVMe checkpoint affinity."
        if tinfo.get("io_contended"):
            return False, "T2 rejected layout due to shared-rack NVMe IO contention with another high-IO batch team."

    elif team_id == "T3":
        if ticket["require_single_spine"] and tinfo.get("cross_spine"):
            return False, "T3 concession requires a single Spine fabric without cross-Spine traffic."
        if ticket["require_no_bleed"] and tinfo.get("thermal_bleed"):
            return False, "T3 concession requires zero vertical High-TDP thermal bleed."

    elif team_id == "T4":
        if ticket["require_priority_iops"] and not tinfo.get("all_iops_ok"):
            return False, "T4 off-peak concession requires priority_iops=true on all assigned bays."
        if tinfo.get("io_contended"):
            return False, "T4 hit storage IO contention on shared rack with T2 during an overlapping shift."

    elif team_id == "T5":
        for a in assigns:
            if ticket["accepted_shift"] == "DAY_ONLY" and a["shift"] == "NIGHT_ONLY":
                return False, "T5 engineering team only operates during DAY_ONLY shift."

    return True, f"{team_id} SLA, fabric, shift, and capacity terms verified -> SIGNED_OFF."


@app.get("/health")
def health():
    return {"status": "ok", "service": "team-desk"}


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


@app.get("/api/tickets")
def list_tickets(user: str = Depends(require_auth)):
    with get_conn() as conn:
        tickets = [dict(r) for r in conn.execute("SELECT * FROM team_tickets ORDER BY team_id")]
        logs = [dict(r) for r in conn.execute("SELECT * FROM negotiation_logs ORDER BY id")]
    by_team = {}
    for l in logs:
        by_team.setdefault(l["team_id"], []).append(l)
    for t in tickets:
        t["logs"] = by_team.get(t["team_id"], [])
    return {"tickets": tickets, "archive": LEGACY_ARCHIVE}


@app.post("/api/tickets/{team_id}/negotiate")
def negotiate_ticket(team_id: str, body: NegotiateRequest, user: str = Depends(require_auth)):
    parsed = _parse_intent(body)
    ts = now_utc8()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM team_tickets WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Unknown team {team_id}")
        ticket = dict(row)
        conn.execute(
            "INSERT INTO negotiation_logs (team_id, sender, message, payload_json, created_at) VALUES (?, 'agent', ?, ?, ?)",
            (team_id, body.message or json.dumps(parsed), json.dumps(parsed), ts),
        )

        reply = ""
        accepted = False
        if team_id == "T1":
            if parsed["rdma"] and (parsed["bays"] is None or parsed["bays"] >= 4):
                conn.execute(
                    "UPDATE team_tickets SET req_bays=4, require_rdma=1, status='NEGOTIATING', feedback=? WHERE team_id='T1'",
                    ("Accepted concession: 16 GPUs (4 bays, 24H) if placed on Spine-B with RDMA enabled.",),
                )
                reply = "T1 代表回复：若能保证 Spine-B RDMA 专网不断连，我们同意从 24 卡压测优化至 16 卡（4 个 24H 槽位）。"
                accepted = True
            else:
                reply = "T1 代表回复：无 RDMA 专网保证时梯度同步耗时过高，我们必须保留 24 卡（6 槽位）；仅当提供 Spine-B RDMA 时可降至 16 卡（4 槽位）。"

        elif team_id == "T2":
            reply = "T2 代表回复：实时搜推无减卡余地，必须占满 16 卡（4 槽位 24H），且所有节点必须留在 Zone-A（R1/R2）使用本地高速 NVMe，严禁与其它高 IO 任务同机柜争抢总线。"

        elif team_id == "T3":
            if (parsed["single_spine"] or parsed["no_bleed"]) and (parsed["bays"] is None or parsed["bays"] >= 3):
                conn.execute(
                    "UPDATE team_tickets SET req_bays=3, require_single_spine=1, require_no_bleed=1, status='NEGOTIATING', feedback=? WHERE team_id='T3'",
                    ("Accepted concession: 12 GPUs (3 bays, 24H) if kept on single Spine with zero thermal bleed.",),
                )
                reply = "T3 代表回复：若确保单 Spine 不跨交换机且无相邻高功耗热降频，我们接受缩减至 12 卡（3 个 24H 槽位）。"
                accepted = True
            else:
                reply = "T3 代表回复：只有同时承诺单 Spine 集中部署且不发生热降频时，多模态后训练才愿意从 16 卡压缩到 12 卡（3 槽位）。"

        elif team_id == "T4":
            if parsed["shift"] == "NIGHT_ONLY" and parsed["iops"] and (parsed["bays"] is None or parsed["bays"] >= 4):
                conn.execute(
                    "UPDATE team_tickets SET req_bays=4, accepted_shift='NIGHT_ONLY', require_priority_iops=1, status='NEGOTIATING', feedback=? WHERE team_id='T4'",
                    ("Accepted NIGHT_ONLY off-peak shift (16 GPUs / 4 NIGHT bays) with priority_iops enabled.",),
                )
                reply = "T4 代表回复：如果把 4 个槽位设为优先 IOPS（priority_iops），风控离线评测可完全切到 NIGHT_ONLY（21:00-09:00）夜间错峰运行。"
                accepted = True
            else:
                reply = "T4 代表回复：风控回测样本量大，只有在分配 4 槽 NIGHT_ONLY 并开启 priority_iops 时才愿让出白天时段。"

        elif team_id == "T5":
            if parsed["shift"] == "DAY_ONLY" and (parsed["bays"] is None or parsed["bays"] >= 4):
                conn.execute(
                    "UPDATE team_tickets SET req_bays=4, accepted_shift='DAY_ONLY', status='NEGOTIATING', feedback=? WHERE team_id='T5'",
                    ("Accepted DAY_ONLY shift (16 GPUs / 4 DAY bays).",),
                )
                reply = "T5 代表回复：算法团队本身集中在白天微调迭代，只要保住 4 槽 DAY_ONLY（09:00-21:00），同意晚上让出硬件槽位给夜间批处理任务。"
                accepted = True
            else:
                reply = "T5 代表回复：若提供 DAY_ONLY 4 个白班槽位（16 卡），我们可以放弃夜间独占。"

        elif team_id == "T6":
            reply = "T6 代表回复：安全红队策略迭代需 12 卡（3 槽位 24H），满载功耗较高，无进一步压缩空间。"

        elif team_id == "T7":
            reply = "T7 代表回复：如需驳回，请核对 FinOps 台 Q4 预算状态（PO-2026-Q4-MKT）后再执行 Defer。"

        elif team_id == "T8":
            reply = "T8 代表回复：请先核对本平台历史工单归档 REQ-2026-089 确认重复申请后再处理。"

        conn.execute(
            "INSERT INTO negotiation_logs (team_id, sender, message, payload_json, created_at) VALUES (?, 'team', ?, ?, ?)",
            (team_id, reply, json.dumps({"accepted": accepted}), ts),
        )
        conn.commit()
        updated = dict(conn.execute("SELECT * FROM team_tickets WHERE team_id=?", (team_id,)).fetchone())
    return {"ok": True, "accepted": accepted, "reply": reply, "ticket": updated}


@app.post("/api/tickets/{team_id}/signoff")
def signoff_ticket(team_id: str, user: str = Depends(require_auth)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM team_tickets WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Unknown team {team_id}")
        ticket = dict(row)
        cur_hash = compute_state_hash(build_sim_payload(conn))
        sim_row = conn.execute("SELECT * FROM simulations ORDER BY run_id DESC LIMIT 1").fetchone()
        if not sim_row or sim_row["state_hash"] != cur_hash:
            raise HTTPException(
                status_code=409,
                detail="No up-to-date Staging simulation matches the current layout and negotiated terms. Run a Staging simulation on Rack Console (:8081) first.",
            )
        sim_raw = json.loads(sim_row["raw_json"])
        ok, msg = verify_team_signoff(team_id, ticket, sim_raw, conn)
        new_status = "SIGNED_OFF" if ok else "VETOED"
        conn.execute("UPDATE team_tickets SET status=?, feedback=? WHERE team_id=?", (new_status, msg, team_id))
        conn.commit()
        updated = dict(conn.execute("SELECT * FROM team_tickets WHERE team_id=?", (team_id,)).fetchone())
    return {"ok": ok, "status": new_status, "feedback": msg, "ticket": updated}


@app.post("/api/tickets/{team_id}/defer")
def defer_ticket(team_id: str, body: DeferRequest, user: str = Depends(require_auth)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM team_tickets WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Unknown team {team_id}")
        assigned = conn.execute("SELECT COUNT(*) FROM allocations WHERE team=?", (team_id,)).fetchone()[0]
        if assigned > 0:
            raise HTTPException(status_code=400, detail=f"Cannot defer {team_id} while {assigned} bays are still assigned in Rack Console.")
        if team_id not in {"T7", "T8"}:
            raise HTTPException(status_code=400, detail=f"{team_id} has valid active Q4 PO budget and cannot be unilaterally deferred.")
        msg = body.reason.strip() or f"{team_id} deferred after FinOps/Archive eligibility check."
        conn.execute("UPDATE team_tickets SET status='DEFERRED', feedback=? WHERE team_id=?", (msg, team_id))
        conn.commit()
        updated = dict(conn.execute("SELECT * FROM team_tickets WHERE team_id=?", (team_id,)).fetchone())
    return {"ok": True, "ticket": updated}


@app.get("/")
def index(request: Request):
    if not verify_session_cookie(request.cookies.get("cluster_session")):
        return RedirectResponse("/login", status_code=302)
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Team Negotiation Desk (:8082)</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:18px}
.top{display:flex;justify-content:space-between;align-items:center;background:#1e293b;padding:14px 20px;border-radius:10px;margin-bottom:16px;border:1px solid #334155}
.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px}
.badge{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:700}
.SIGNED_OFF{background:#166534;color:#dcfce7}.DEFERRED{background:#475569;color:#f1f5f9}.VETOED{background:#991b1b;color:#fee2e2}.OPEN,.NEGOTIATING{background:#1d4ed8;color:#dbeafe}
.btn{background:#2563eb;color:#fff;border:none;padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px}
.btn.ok{background:#16a34a}.btn.gray{background:#475569}
input{background:#0f172a;color:#fff;border:1px solid #475569;padding:6px;border-radius:5px;width:65%}
.arch{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;margin-top:16px}
</style></head><body>
<div class="top">
  <div><h2 style="margin:0">Team Demand &amp; Negotiation Desk (:8082)</h2><div style="color:#94a3b8;font-size:13px">协商错峰、压缩卡数与发起 SLA 签收</div></div>
  <div style="display:flex;gap:12px"><a href="http://127.0.0.1:8081/" target="_blank" style="color:#93c5fd">Rack Console (:8081)</a><a href="http://127.0.0.1:8083/" target="_blank" style="color:#93c5fd">FinOps (:8083)</a></div>
</div>
<div id="cards" class="cards"></div>
<div class="arch"><h3>历史审批与事故归档</h3><pre id="arch" style="color:#cbd5e1"></pre></div>
<script>
async function load(){
  const r = await fetch('/api/tickets'); const d = await r.json();
  document.getElementById('arch').textContent = JSON.stringify(d.archive, null, 2);
  const c = document.getElementById('cards'); c.innerHTML = '';
  for(const t of d.tickets){
    const div = document.createElement('div'); div.className = 'card';
    const logs = t.logs.map(l => `[${l.sender}] ${l.message}`).join('\n');
    div.innerHTML = `<div style="display:flex;justify-content:space-between"><strong>${t.team_id} · ${t.team_name}</strong><span class="badge ${t.status}">${t.status}</span></div>
    <div style="font-size:12px;color:#94a3b8;margin:6px 0">TDP: ${t.tdp_class} (${t.tdp_kw}kW/bay) | 需求: ${t.req_bays} bays (${t.req_bays*4} GPUs) | 班次: ${t.accepted_shift}</div>
    <div style="font-size:13px;margin-bottom:8px">${t.summary}</div>
    <div style="font-size:12px;color:#fcd34d;margin-bottom:8px">${t.feedback || ''}</div>
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <input id="msg-${t.team_id}" placeholder="输入协商方案，如：提供Spine-B RDMA降至16卡">
      <button class="btn" onclick="neg('${t.team_id}')">协商</button>
      <button class="btn ok" onclick="signoff('${t.team_id}')">签收</button>
      <button class="btn gray" onclick="deferT('${t.team_id}')">挂起</button>
    </div>
    <pre style="background:#0f172a;padding:8px;border-radius:6px;font-size:11px;max-height:90px;overflow:auto;white-space:pre-wrap">${logs}</pre>`;
    c.appendChild(div);
  }
}
async function neg(id){
  const msg = document.getElementById('msg-'+id).value;
  await fetch('/api/tickets/'+id+'/negotiate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  load();
}
async function signoff(id){
  const r = await fetch('/api/tickets/'+id+'/signoff',{method:'POST'});
  const d = await r.json(); if(!d.ok) alert(d.feedback); load();
}
async function deferT(id){
  const r = await fetch('/api/tickets/'+id+'/defer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'Deferred via Desk UI'})});
  const d = await r.json(); if(!r.ok) alert(d.detail); load();
}
load();
</script></body></html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORTAL_PORT", "8082")))
