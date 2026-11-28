import json
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from common import (
    PORTAL_PASS,
    PORTAL_USER,
    build_sim_payload,
    compute_state_hash,
    compute_tariff_cost,
    get_conn,
    make_session_cookie,
    now_utc8,
    require_auth,
    verify_session_cookie,
)

app = FastAPI(title="FinOps & Change Release Portal", version="0.6.4")

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Login - FinOps &amp; Release Portal</title>
<style>body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e293b;padding:28px;border-radius:12px;width:360px;border:1px solid #334155}
input,button{width:100%;padding:10px;margin:8px 0;border-radius:6px;box-sizing:border-box}
input{background:#0f172a;color:#f8fafc;border:1px solid #475569}
button{background:#2563eb;color:#fff;border:none;font-weight:600;cursor:pointer}</style></head>
<body><form class="card" method="post" action="/login">
<h2>FinOps &amp; Change Release</h2><p style="color:#94a3b8;font-size:13px">算力预算 PO 台账、费用核算与正式变更签发台</p>
<input name="username" placeholder="Username" value="cap_lead" required>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">登录结算与发布台</button></form></body></html>"""


@app.get("/health")
def health():
    return {"status": "ok", "service": "finops-cab"}


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


@app.get("/api/finops/ledger")
def get_ledger(user: str = Depends(require_auth)):
    with get_conn() as conn:
        pos = [dict(r) for r in conn.execute("SELECT * FROM finops_po ORDER BY team_id")]
        assigns = [dict(r) for r in conn.execute("SELECT rack, bay, shift, team, rdma, priority_iops FROM allocations ORDER BY rack, bay, shift")]
        commits = [dict(r) for r in conn.execute("SELECT commit_id, release_code, total_monthly_cost_usd, committed_at FROM release_commits ORDER BY commit_id DESC")]
    draft_cost = compute_tariff_cost(assigns)
    return {
        "po_ledger": pos,
        "tariff": {
            "day_only_usd": 3800.0,
            "night_only_usd": 2400.0,
            "shift_24h_usd": 6200.0,
            "rdma_surcharge_usd": 400.0,
            "t2_cross_zone_egress_usd": 4000.0,
            "monthly_budget_cap_usd": 138000.0,
        },
        "draft_cost": draft_cost,
        "commits": commits,
    }


@app.post("/api/release/commit")
def commit_release(user: str = Depends(require_auth)):
    with get_conn() as conn:
        payload = build_sim_payload(conn)
        cur_hash = compute_state_hash(payload)
        sim_row = conn.execute("SELECT * FROM simulations ORDER BY run_id DESC LIMIT 1").fetchone()
        if not sim_row or sim_row["state_hash"] != cur_hash:
            raise HTTPException(
                status_code=409,
                detail="Cannot commit unverified layout. Run a Staging simulation on Rack Console (:8081) matching the current layout and negotiated terms first.",
            )
        sim_raw = json.loads(sim_row["raw_json"])
        if not sim_raw["hard_feasible"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot commit cluster release: status={sim_raw['status']}, cost=${sim_raw['monthly_cost_usd']:.2f}",
            )
        assigns = payload["assignments"]
        tickets = [dict(r) for r in conn.execute("SELECT * FROM team_tickets ORDER BY team_id")]
        ts = now_utc8()
        cur = conn.execute(
            "INSERT INTO release_commits (release_code, total_monthly_cost_usd, u_total, allocations_json, tickets_json, sim_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("PENDING", sim_raw["monthly_cost_usd"], sim_raw["u_total"], json.dumps(assigns), json.dumps(tickets), json.dumps(sim_raw), ts),
        )
        cid = cur.lastrowid
        rcode = f"REL-2026Q4-{cid:03d}"
        conn.execute("UPDATE release_commits SET release_code=? WHERE commit_id=?", (rcode, cid))
        conn.commit()
    return {
        "ok": True,
        "release_code": rcode,
        "committed_at": ts,
        "total_monthly_cost_usd": sim_raw["monthly_cost_usd"],
        "budget_cap_usd": sim_raw["budget_cap_usd"],
        "status": sim_raw["status"],
    }


@app.get("/")
def index(request: Request):
    if not verify_session_cookie(request.cookies.get("cluster_session")):
        return RedirectResponse("/login", status_code=302)
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>FinOps &amp; Release Portal (:8083)</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:18px}
.top{display:flex;justify-content:space-between;align-items:center;background:#1e293b;padding:14px 20px;border-radius:10px;margin-bottom:16px;border:1px solid #334155}
.panel{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:16px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid #334155;padding:8px;text-align:left}
.btn{background:#16a34a;color:#fff;border:none;padding:10px 16px;border-radius:6px;cursor:pointer;font-weight:700}
</style></head><body>
<div class="top">
  <div><h2 style="margin:0">FinOps &amp; Change Release Portal (:8083)</h2><div style="color:#94a3b8;font-size:13px">PO 合规核查、月度电费核算与变更签发</div></div>
  <div style="display:flex;gap:12px;align-items:center">
    <a href="http://127.0.0.1:8081/" target="_blank" style="color:#93c5fd">Rack Console (:8081)</a>
    <a href="http://127.0.0.1:8082/" target="_blank" style="color:#93c5fd">Team Desk (:8082)</a>
    <button class="btn" onclick="commitRel()">正式签发落库</button>
  </div>
</div>
<div class="panel"><h3>Q4 部门预算 PO 台账（月度总上限 $138,000）</h3><table><thead><tr><th>Team</th><th>PO Code</th><th>Status</th><th>Budget USD</th><th>Notes</th></tr></thead><tbody id="po"></tbody></table></div>
<div class="panel"><h3>当前排布月度费用明细</h3><pre id="cost" style="background:#0f172a;padding:10px;border-radius:6px"></pre></div>
<div class="panel"><h3>已签发变更记录</h3><pre id="rel" style="background:#0f172a;padding:10px;border-radius:6px"></pre></div>
<script>
async function load(){
  const r = await fetch('/api/finops/ledger'); const d = await r.json();
  document.getElementById('po').innerHTML = d.po_ledger.map(p => `<tr><td>${p.team_id}</td><td>${p.po_code}</td><td style="color:${p.po_status==='ACTIVE'?'#86efac':'#fca5a5'}">${p.po_status}</td><td>$${p.approved_budget_usd}</td><td>${p.notes}</td></tr>`).join('');
  document.getElementById('cost').textContent = JSON.stringify(d.draft_cost, null, 2);
  document.getElementById('rel').textContent = JSON.stringify(d.commits, null, 2);
}
async function commitRel(){
  const r = await fetch('/api/release/commit',{method:'POST'});
  const d = await r.json(); if(!r.ok) alert(d.detail||JSON.stringify(d)); else { alert('Committed: '+d.release_code); load(); }
}
load();
</script></body></html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORTAL_PORT", "8083")))
