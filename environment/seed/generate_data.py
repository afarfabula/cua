#!/usr/bin/env python3
"""v2 Deterministic generator:
- Binary Perfetto .pftrace (6 processes, 28 threads, 10,000+ noisy realistic slices, ZERO spoiler strings)
- Multi-noise 7-panel Prometheus OpenMetrics
- Visual Mailpit attachments: DBA lock PNG, Draft-VOID PDF, Red-Sealed REV-2 PDF & PNG, Color SLA Corridor Matrix PNG
- Workspace Contract PDF (vip_merchant_framework_2026.pdf with MCH_VIP_003 00:00-01:00 UTC+8 exemption)
- Mixed-timezone gateway_requests.jsonl & settlement_batches_raw.csv + protected ground_truth.json
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import random
from decimal import Decimal, ROUND_HALF_UP

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from pftrace_encoder import PftraceWriter

TZ_UTC8 = dt.timezone(dt.timedelta(hours=8))
TZ_UTC = dt.timezone.utc

FX_RATES = {
    "CNY": Decimal("1.00"),
    "USD": Decimal("7.15"),
    "HKD": Decimal("0.92"),
}

MERCHANTS = {
    "MCH_VIP_001": {"tier": "VIP_T1", "name": "Apex CrossBorder Retail"},
    "MCH_VIP_002": {"tier": "VIP_T1", "name": "Pacific DutyFree Group"},
    "MCH_STD_010": {"tier": "STD", "name": "Harbor Electronics Ltd"},
    "MCH_VIP_003": {"tier": "VIP_T1", "name": "Meridian Luxury Pay"},
}


def q2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def generate_visual_and_contract_docs(seed_dir: pathlib.Path, contracts_dir: pathlib.Path) -> None:
    seed_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    font_h = ImageFont.load_default(size=20)
    font_b = ImageFont.load_default(size=16)
    font_s = ImageFont.load_default(size=14)

    # 1. DBA PostgreSQL Lock-Wait & Connection Pool Snapshot (PNG)
    img1 = Image.new("RGB", (1040, 580), (248, 250, 252))
    d1 = ImageDraw.Draw(img1)
    d1.rectangle([20, 20, 1020, 90], fill=(15, 23, 42))
    d1.text((40, 42), "DBA INCIDENT SNAPSHOT: pg-primary-01 Lock & Connection Pool Monitor", fill=(255, 255, 255), font=font_h)

    d1.rectangle([40, 120, 500, 330], fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    d1.text((60, 138), "PostgreSQL Cluster: pg-primary-01", fill=(15, 23, 42), font=font_b)
    d1.text((60, 178), "max_connections       = 180  (HARD LIMIT)", fill=(185, 28, 28), font=font_b)
    d1.text((60, 210), "active_connections    = 168  (93.3% SATURATED)", fill=(185, 28, 28), font=font_b)
    d1.text((60, 242), "waiting_on_row_lock   = 44 sessions", fill=(51, 65, 85), font=font_b)
    d1.text((60, 274), "idle_in_transaction   = 39 sessions (risk-engine)", fill=(51, 65, 85), font=font_b)

    d1.rectangle([525, 120, 1000, 330], fill=(254, 242, 242), outline=(220, 38, 38), width=3)
    d1.text((545, 140), "DBA WARNING ON GITEA PR #1:", fill=(185, 28, 28), font=font_b)
    d1.text((545, 180), "DO NOT MERGE PR #1 (db_pool_size=300)!", fill=(153, 27, 27), font=font_b)
    d1.text((545, 218), "Raising pool_size above max_connections=180", fill=(51, 65, 85), font=font_s)
    d1.text((545, 246), "will exhaust Postgres slots and take down", fill=(51, 65, 85), font=font_s)
    d1.text((545, 274), "settle-node-01 and pg-primary-01 completely.", fill=(51, 65, 85), font=font_s)

    d1.rectangle([40, 355, 1000, 545], fill=(255, 255, 255), outline=(148, 163, 184), width=2)
    d1.text((60, 372), "Observed Lock Cascade (2026-11-24 14:14 -> 14:20 UTC+8):", fill=(15, 23, 42), font=font_b)
    d1.text((60, 412), "[risk-node-02/03 v2.8.4] (lock contention) ---> [settlement-worker] (idempotency retry storm)", fill=(30, 64, 175), font=font_s)
    d1.text((60, 446), "     --> [pg-primary-01] (168/180 conns held) ---> [api-gateway] (504 timeouts at 14:20)", fill=(30, 64, 175), font=font_s)
    d1.text((60, 488), "Note: risk-node-01 (canary v2.8.3) & settle-node-01/02 are victims/healthy.", fill=(21, 128, 61), font=font_s)
    img1.save(seed_dir / "dba_pg_lock_graph.png")

    # 2. Color-Coded Visual SLA & Corridor Matrix (PNG) - spatial/color reasoning!
    img_mat = Image.new("RGB", (1040, 560), (255, 255, 255))
    dm = ImageDraw.Draw(img_mat)
    dm.rectangle([20, 20, 1020, 82], fill=(30, 41, 59))
    dm.text((40, 40), "VISUAL SLA & CHANGE CORRIDOR MATRIX (EFFECTIVE 2026-11-24)", fill=(255, 255, 255), font=font_h)

    # Legend color boxes (stacked vertically so zero text clips off-canvas)
    dm.rectangle([40, 96, 70, 118], fill=(220, 38, 38))
    dm.text((82, 98), "RED = Tightened 600ms / 2.5% (Cap 5000 CNY) or EOD Freeze Window", fill=(153, 27, 27), font=font_s)
    dm.rectangle([40, 128, 70, 150], fill=(245, 158, 11))
    dm.text((82, 130), "AMBER = Contractual Maintenance Exemption (00:00-01:00 UTC+8)", fill=(146, 64, 14), font=font_s)
    dm.rectangle([40, 160, 70, 182], fill=(22, 163, 74))
    dm.text((82, 162), "GREEN = Standard 1500ms / 0.5% (Uncapped) or Allowed Release Slot", fill=(22, 101, 52), font=font_s)

    # Grid of merchant cards (color coded)
    cards = [
        (40, 204, 505, 326, (254, 226, 226), (220, 38, 38), "MCH_VIP_001 (VIP_T1 | USD | FX=7.15)", "RED SLA: >600ms or Err -> 2.5%, Cap 5000 CNY"),
        (535, 204, 1000, 326, (254, 226, 226), (220, 38, 38), "MCH_VIP_002 (VIP_T1 | CNY | FX=1.00)", "RED SLA: >600ms or Err -> 2.5%, Cap 5000 CNY"),
        (40, 348, 505, 470, (220, 252, 231), (22, 163, 74), "MCH_STD_010 (STD | HKD | FX=0.92)", "GREEN SLA: >1500ms or Err -> 0.5%, Uncapped"),
        (535, 348, 1000, 470, (254, 243, 199), (245, 158, 11), "MCH_VIP_003 (VIP_T1 | USD | FX=7.15)", "RED SLA + AMBER CLAUSE 4.2: 00:00-01:00 UTC+8 Exempt"),
    ]
    for x1, y1, x2, y2, bg, bd, title, sub in cards:
        dm.rectangle([x1, y1, x2, y2], fill=bg, outline=bd, width=3)
        dm.text((x1 + 18, y1 + 22), title, fill=(15, 23, 42), font=font_b)
        dm.text((x1 + 18, y1 + 64), sub, fill=(30, 41, 59), font=font_s)
    dm.rectangle([40, 492, 1000, 536], fill=(241, 245, 249), outline=(148, 163, 184), width=2)
    dm.text((56, 504), "EOD Release Freeze Strip: [17:30 ==== RED FROZEN ==== 19:30]  |  [19:30 ==== GREEN OPEN ==== 23:59]", fill=(15, 23, 42), font=font_b)
    img_mat.save(seed_dir / "sla_corridor_matrix.png")

    # 3. Red-Sealed REV-2 Compliance Errata (PNG)
    img2 = Image.new("RGB", (1040, 640), (255, 253, 247))
    d2 = ImageDraw.Draw(img2)
    d2.rectangle([25, 25, 1015, 615], outline=(127, 29, 29), width=4)
    d2.rectangle([35, 35, 1005, 105], fill=(153, 27, 27))
    d2.text((55, 58), "COMPLIANCE & CAB URGENT ERRATA NOTICE (EFFECTIVE 2026-11-24 00:00 UTC+8)", fill=(255, 255, 255), font=font_h)

    lines = [
        "Document ID: COMP-ERRATA-20261124-REV2   (Supersedes sla_policy_v1.md & Draft v1.5)",
        "",
        "1. VIP Tier-1 (tier = VIP_T1) SLA Override:",
        "   - Latency Threshold  : 600 ms   (tightened from 1000 ms in v1.0)",
        "   - Penalty Rate       : 2.5% (0.025) of settled CNY amount per breached tx",
        "   - Per-Batch Cap      : 5000.00 CNY maximum penalty per batch",
        "",
        "2. Standard Tier (tier = STD) remains 1500 ms threshold, 0.5% (0.005), uncapped.",
        "",
        "3. Fixed Settlement FX Conversion Rates for 2026-11-24:",
        "   - 1 USD = 7.15 CNY  |  1 HKD = 0.92 CNY  |  1 CNY = 1.00 CNY",
        "   (tx_cny = round_half_up(amount_orig * fx, 2); batch_pen = min(cap, round_half_up(sum(tx_cny * rate), 2)))",
        "",
        "4. Mandatory Daily EOD Clearing Change Freeze Window:",
        "   - Production rollouts for risk-engine / settlement-worker are FORBIDDEN",
        "     between 17:30 and 19:30 UTC+8 (supersedes 20:00-22:00 in v1.0).",
    ]
    y = 128
    for line in lines:
        color = (153, 27, 27) if ("600 ms" in line or "2.5%" in line or "5000.00" in line or "17:30" in line or "7.15" in line or "0.92" in line) else (30, 41, 59)
        d2.text((55, y), line, fill=color, font=font_s)
        y += 27

    d2.ellipse([780, 420, 975, 585], outline=(220, 38, 38), width=5)
    d2.ellipse([788, 428, 967, 577], outline=(220, 38, 38), width=2)
    d2.text((818, 470), "APPROVED SEAL", fill=(220, 38, 38), font=font_b)
    d2.text((806, 500), "RISK & COMPLIANCE", fill=(220, 38, 38), font=font_s)
    d2.text((836, 528), "2026-11-24", fill=(220, 38, 38), font=font_b)
    img2.save(seed_dir / "sla_errata_stamp.png")

    # 4. Formal Sealed REV-2 PDF
    c = canvas.Canvas(str(seed_dir / "compliance_errata_20261124.pdf"), pagesize=A4)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(50, 790, "COMPLIANCE & CAB URGENT ERRATA NOTICE (REV-2)")
    c.setFont("Helvetica", 11)
    pdf_lines = [
        "Document ID: COMP-ERRATA-20261124-REV2 | Effective: 2026-11-24 00:00:00+08:00",
        "This sealed notice supersedes sla_policy_v1.md and unsigned Draft v1.5:",
        "",
        "1. VIP_T1 SLA Threshold: 600 ms (previously 1000 ms in v1.0).",
        "2. VIP_T1 Penalty Rate: 2.5% (0.025) of settled CNY amount, capped at 5000.00 CNY per batch.",
        "3. STD SLA Threshold: 1500 ms, Penalty Rate: 0.5% (0.005), no batch cap.",
        "4. Settlement FX Rates (2026-11-24): 1 USD = 7.15 CNY | 1 HKD = 0.92 CNY | 1 CNY = 1.00 CNY.",
        "5. Rounding: tx_cny = round_half_up(amount_orig * fx_rate, 2).",
        "   batch_penalty = min(cap, round_half_up(sum(tx_cny * rate for breached txs), 2)).",
        "6. Mandatory EOD Change Freeze Window: 17:30 - 19:30 UTC+8 daily.",
        "7. Sandbox Exclusion: Any merchant_id prefixed with TEST_ is excluded from SLA & batches.",
    ]
    py = 755
    for line in pdf_lines:
        c.drawString(50, py, line)
        py -= 20
    c.setStrokeColorRGB(0.85, 0.1, 0.1)
    c.setLineWidth(2)
    c.circle(470, 520, 48)
    c.setFillColorRGB(0.85, 0.1, 0.1)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(470, 525, "APPROVED SEAL")
    c.drawCentredString(470, 512, "2026-11-24")
    c.save()

    # 5. Distractor Draft PDF (VOID / SUPERSEDED watermark, NO seal!)
    c_void = canvas.Canvas(str(seed_dir / "sla_draft_v1_5_void.pdf"), pagesize=A4)
    c_void.setFont("Helvetica-Bold", 15)
    c_void.drawString(50, 790, "INTERNAL DISCUSSION DRAFT v1.5 (NOT APPROVED)")
    c_void.setFont("Helvetica", 11)
    for idx, line in enumerate([
        "Proposed VIP_T1 SLA Threshold: 800 ms | Penalty Rate: 2.0% | Cap: 4000.00 CNY",
        "Proposed EOD Freeze Window: 19:00 - 21:00 UTC+8",
        "NOTE: Unsigned draft superseded by COMP-ERRATA-20261124-REV2.",
    ]):
        c_void.drawString(50, 750 - idx * 22, line)
    c_void.saveState()
    c_void.translate(300, 420)
    c_void.rotate(35)
    c_void.setFillColorRGB(0.65, 0.65, 0.65)
    c_void.setFont("Helvetica-Bold", 42)
    c_void.drawCentredString(0, 0, "DRAFT - VOID / SUPERSEDED")
    c_void.restoreState()
    c_void.save()

    # 6. Workspace VIP Merchant Framework Contract PDF (Clause 4.2 MCH_VIP_003 exemption!)
    c_con = canvas.Canvas(str(contracts_dir / "vip_merchant_framework_2026.pdf"), pagesize=A4)
    c_con.setFont("Helvetica-Bold", 15)
    c_con.drawString(50, 790, "CROSS-BORDER VIP MERCHANT MASTER FRAMEWORK CONTRACT (2026)")
    c_con.setFont("Helvetica", 11)
    con_lines = [
        "Contract No: FIN-MSA-2026-VIP-088 | Parties: Clearing Core Platform & VIP Merchants",
        "",
        "Clause 3.1 - Batch Settlement Cut-off:",
        "   All settlement batches are bounded by calendar day in Asia/Shanghai (UTC+8).",
        "   Logs containing UTC ('Z') timestamps must be converted to UTC+8 prior to date filtering.",
        "",
        "Clause 4.2 - Scheduled Bank Maintenance Window Exemption (MCH_VIP_003 ONLY):",
        "   Due to Meridian Luxury Pay (MCH_VIP_003) overseas correspondent bank reconciliation,",
        "   transactions for MCH_VIP_003 whose final log timestamp falls within 00:00:00 to 00:59:59",
        "   UTC+8 on any day SHALL STILL BE INCLUDED in settled_amount_cny, but ARE EXEMPT from",
        "   SLA latency breach counting and SLA penalty calculation (unless flagged as duplicate risk).",
        "",
        "Clause 5.4 - Duplicate Debit Risk Reporting:",
        "   Any tx_id that recorded SETTLED on an earlier attempt and underwent a subsequent retry",
        "   attempt must be reported under duplicate_risk_tx_ids and batch duplicate_risk_count.",
    ]
    py = 755
    for line in con_lines:
        c_con.drawString(50, py, line)
        py -= 20
    c_con.save()


def generate_complex_pftrace(trace_path: pathlib.Path) -> None:
    """Generate authentic binary .pftrace with 6 processes, 28 threads, 10,000+ slices,
    heavy background noise, and ZERO spoiler text!
    """
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    w = PftraceWriter(seq_id=2026)

    # 6 Service Processes
    procs = [
        (1001, 101, "api-gateway (v3.1.0)"),
        (1002, 102, "risk-engine (v2.8.4)"),
        (1003, 103, "settlement-worker (v2.3.1)"),
        (1004, 104, "fx-quote-proxy (v1.4.2)"),
        (1005, 105, "pg-bouncer (v1.22)"),
        (1006, 106, "redis-sentinel (v7.2)"),
    ]
    for u, pid, name in procs:
        w.add_process_track(u, pid, name)

    # 28 Thread Swimlanes
    threads = {}
    uid = 2000
    def reg(pu, pid, tid, tname):
        nonlocal uid
        uid += 1
        w.add_thread_track(uid, pu, pid, tid, tname)
        threads[tname] = uid

    for i in range(4):
        reg(1001, 101, 110 + i, f"gw-http-worker-{i}")
    reg(1001, 101, 118, "gw-healthcheck")
    reg(1001, 101, 119, "gw-metrics-flush")

    for i in range(8):
        reg(1002, 102, 210 + i, f"risk-worker-{i}")
    reg(1002, 102, 220, "risk-rule-sync")
    reg(1002, 102, 221, "risk-gc-daemon")
    reg(1002, 102, 222, "risk-metrics-pusher")

    for i in range(6):
        reg(1003, 103, 310 + i, f"settle-dispatcher-{i}")
    reg(1003, 103, 320, "settle-recon-cron")
    reg(1003, 103, 321, "settle-pg-keepalive")

    for i in range(3):
        reg(1004, 104, 410 + i, f"fx-handler-{i}")
    reg(1004, 104, 420, "fx-cache-warmer")

    for i in range(3):
        reg(1005, 105, 510 + i, f"pgb-pool-{i}")
    for i in range(2):
        reg(1006, 106, 610 + i, f"redis-heartbeat-{i}")

    rng = random.Random(20261124)
    t0_ns = 1_000_000_000  # 1s base

    # 1) Background noise across all 45s (healthchecks, GC, metrics, redis ping, kafka poll, normal fast traffic)
    for sec in range(45):
        base = t0_ns + sec * 1_000_000_000
        # Healthcheck & Redis & PG keepalive every 250ms
        for sub in range(4):
            ts = base + sub * 250_000_000
            w.add_slice(threads["gw-healthcheck"], ts, 1_200_000, "http.get(/healthz)", "net")
            w.add_slice(threads["redis-heartbeat-0"], ts + 5_000_000, 800_000, "redis.PING", "cache")
            w.add_slice(threads["redis-heartbeat-1"], ts + 12_000_000, 900_000, "sentinel.info", "cache")
            w.add_slice(threads["settle-pg-keepalive"], ts + 20_000_000, 2_100_000, "pg.SELECT_1", "db")
            w.add_slice(threads["fx-cache-warmer"], ts + 30_000_000, 3_500_000, "fx.cache_stat", "cache")
        # Metrics flush & rule sync & GC pauses every second
        w.add_slice(threads["gw-metrics-flush"], base + 100_000_000, 8_000_000, "prometheus.scrape_collect", "telemetry")
        w.add_slice(threads["risk-metrics-pusher"], base + 150_000_000, 9_500_000, "metrics.export_batch", "telemetry")
        w.add_slice(threads["risk-rule-sync"], base + 200_000_000, 12_000_000, "rules.poll_snapshot", "config")
        if sec % 3 == 0:
            w.add_slice(threads["risk-gc-daemon"], base + 400_000_000, 18_000_000, "gc.minor_sweep", "runtime")
        if sec % 5 == 0:
            w.add_slice(threads["settle-recon-cron"], base + 500_000_000, 42_000_000, "recon.checkpoint_scan", "batch")

        # Healthy baseline requests across gw, risk, settle, pgbouncer (hundreds per sec -> ~9,000+ slices!)
        if sec < 15 or sec >= 36:
            for k in range(35):
                ts = base + k * 27_000_000 + rng.randint(0, 4_000_000)
                gw_t = threads[f"gw-http-worker-{k % 4}"]
                rw_t = threads[f"risk-worker-{k % 8}"]
                sw_t = threads[f"settle-dispatcher-{k % 6}"]
                pg_t = threads[f"pgb-pool-{k % 3}"]
                w.add_slice(gw_t, ts, 14_000_000, "GatewayHandler::handle", "http", {"status": 200})
                w.add_slice(rw_t, ts + 2_000_000, 6_000_000, "RiskEngine::evaluate_tx", "risk", {"pair": "USD_CNY"})
                w.add_slice(rw_t, ts + 2_500_000, 1_200_000, "RiskEngine::get_fx_rate", "risk")
                w.add_slice(sw_t, ts + 9_000_000, 4_500_000, "SettlementDispatcher::submit", "settlement")
                w.add_slice(pg_t, ts + 10_000_000, 2_500_000, "pg.INSERT_settlement", "db")

    # 2) Fault window (sec 15..35, corresponding to 14:14:00+08:00 onset):
    # risk-engine workers serialize one-by-one on Mutex::acquire(_rule_cache_lock) while calling FxClient::refresh_fx_quote!
    lock_free_ns = t0_ns + 15 * 1_000_000_000
    for idx in range(42):
        arr_ns = t0_ns + 15 * 1_000_000_000 + idx * 45_000_000
        rw_t = threads[f"risk-worker-{idx % 8}"]
        gw_t = threads[f"gw-http-worker-{idx % 4}"]
        sw_t = threads[f"settle-dispatcher-{idx % 6}"]
        fx_t = threads[f"fx-handler-{idx % 3}"]
        pg_t = threads[f"pgb-pool-{idx % 3}"]

        acq_ns = max(arr_ns + 400_000, lock_free_ns)
        wait_ns = acq_ns - arr_ns
        if wait_ns > 500_000:
            w.add_slice(rw_t, arr_ns + 300_000, wait_ns - 300_000, "Mutex::wait", "lock", {"mutex": "_rule_cache_lock"})

        hold_ns = 465_000_000  # 465ms held under lock during 3x 503 retries
        total_eval_ns = wait_ns + hold_ns + 3_000_000
        w.add_slice(rw_t, arr_ns, total_eval_ns, "RiskEngine::evaluate_tx", "risk", {"tx_id": f"TX-20261124-{idx+1:04d}"})
        w.add_slice(rw_t, acq_ns, hold_ns, "acquire_rule_lock", "lock", {"mutex": "_rule_cache_lock"})
        w.add_slice(rw_t, acq_ns + 200_000, hold_ns - 400_000, "FxClient::refresh_fx_quote", "risk", {"pair": "USD_CNY"})
        for r in range(3):
            ht = acq_ns + 1_000_000 + r * 153_000_000
            w.add_slice(rw_t, ht, 148_000_000, "fx_quote_http_retry", "net", {"attempt": r + 1, "http_status": 503})
            w.add_slice(fx_t, ht + 2_000_000, 140_000_000, "FxProxy::upstream_quote", "net", {"status": 503})

        w.add_slice(rw_t, acq_ns + hold_ns + 200_000, 1_800_000, "RiskEngine::generate_idempotency_key", "risk", {"idem_prefix": f"MCH_VIP_001:2026-11-24T14:14"})
        lock_free_ns = acq_ns + hold_ns

        # Downstream settlement-worker & pg-bouncer contention & gateway 504s
        if idx >= 6:
            st_ns = arr_ns + wait_ns + 20_000_000
            w.add_slice(sw_t, st_ns, 380_000_000, "SettlementDispatcher::upsert_batch", "settlement", {"pool_active": 168})
            w.add_slice(pg_t, st_ns + 10_000_000, 350_000_000, "pg.row_lock_wait", "db", {"max_connections": 180})
        if idx >= 12:
            w.add_slice(gw_t, arr_ns, min(2_000_000_000, total_eval_ns), "GatewayHandler::upstream_timeout", "http", {"status": 504})

    trace_path.write_bytes(w.to_bytes())


def generate_openmetrics(prom_file: pathlib.Path) -> None:
    """7-panel multi-noise Prometheus OpenMetrics covering 2026-11-24 12:00..18:00 UTC+8."""
    prom_file.parent.mkdir(parents=True, exist_ok=True)
    start_ts = int(dt.datetime(2026, 11, 24, 12, 0, 0, tzinfo=TZ_UTC8).timestamp())
    end_ts = int(dt.datetime(2026, 11, 24, 18, 0, 0, tzinfo=TZ_UTC8).timestamp())

    t_benign_deploy = int(dt.datetime(2026, 11, 24, 12, 30, 0, tzinfo=TZ_UTC8).timestamp())
    t_cron_blip = int(dt.datetime(2026, 11, 24, 13, 10, 0, tzinfo=TZ_UTC8).timestamp())
    t_deploy = int(dt.datetime(2026, 11, 24, 14, 10, 0, tzinfo=TZ_UTC8).timestamp())
    t_risk_onset = int(dt.datetime(2026, 11, 24, 14, 14, 0, tzinfo=TZ_UTC8).timestamp())
    t_settle_onset = int(dt.datetime(2026, 11, 24, 14, 17, 0, tzinfo=TZ_UTC8).timestamp())
    t_gw_onset = int(dt.datetime(2026, 11, 24, 14, 20, 0, tzinfo=TZ_UTC8).timestamp())
    t_mitigate = int(dt.datetime(2026, 11, 24, 16, 45, 0, tzinfo=TZ_UTC8).timestamp())

    lines = [
        "# TYPE clearing_http_request_duration_p99_ms gauge",
        "# TYPE clearing_node_p99_latency_ms gauge",
        "# TYPE clearing_http_error_rate_percent gauge",
        "# TYPE clearing_mutex_contention_wait_ms gauge",
        "# TYPE clearing_pg_active_connections gauge",
        "# TYPE clearing_deploy_event gauge",
    ]
    rng = random.Random(20261124)
    for ts in range(start_ts, end_ts + 1, 60):
        cron_noise = 180.0 if abs(ts - t_cron_blip) <= 120 else 0.0

        # risk-engine spikes FIRST at 14:14 (specifically risk-node-02 & risk-node-03; canary risk-node-01 stays healthy!)
        if ts < t_risk_onset:
            risk_p99 = 85 + cron_noise * 0.2 + rng.uniform(-8, 10)
            n1_p99 = 82 + rng.uniform(-5, 6)
            n2_p99 = 86 + rng.uniform(-5, 6)
            n3_p99 = 84 + rng.uniform(-5, 6)
            risk_err = 0.1
            mutex_wait = 2.0 + rng.uniform(0, 1.5)
        elif ts < t_mitigate:
            ramp = min(1.0, (ts - t_risk_onset) / 300.0)
            risk_p99 = 120 + ramp * 1450 + rng.uniform(-40, 80)
            n1_p99 = 88 + rng.uniform(-6, 8)
            n2_p99 = 135 + ramp * 1510 + rng.uniform(-30, 60)
            n3_p99 = 130 + ramp * 1470 + rng.uniform(-30, 60)
            risk_err = 1.5 + ramp * 14.0
            mutex_wait = 45 + ramp * 470 + rng.uniform(-15, 25)
        else:
            risk_p99 = 240 + rng.uniform(-20, 30)
            n1_p99 = 85 + rng.uniform(-5, 6)
            n2_p99 = 260 + rng.uniform(-20, 25)
            n3_p99 = 250 + rng.uniform(-20, 25)
            risk_err = 0.8
            mutex_wait = 12 + rng.uniform(0, 5)

        if ts < t_settle_onset:
            settle_p99 = 110 + cron_noise + rng.uniform(-10, 12)
            settle_err = 0.05
            pg_conns = 42 + rng.randint(-3, 4)
        elif ts < t_mitigate:
            ramp = min(1.0, (ts - t_settle_onset) / 360.0)
            settle_p99 = 160 + ramp * 1680 + rng.uniform(-50, 90)
            settle_err = 1.0 + ramp * 18.0
            pg_conns = int(50 + ramp * 118)
        else:
            settle_p99 = 190 + rng.uniform(-15, 20)
            settle_err = 0.4
            pg_conns = 58 + rng.randint(-4, 4)

        if ts < t_gw_onset:
            gw_p99 = 140 + cron_noise * 0.4 + rng.uniform(-10, 15)
            gw_err = 0.1
        elif ts < t_mitigate:
            ramp = min(1.0, (ts - t_gw_onset) / 240.0)
            gw_p99 = 250 + ramp * 2400 + rng.uniform(-80, 120)
            gw_err = 2.0 + ramp * 34.0
        else:
            gw_p99 = 280 + rng.uniform(-20, 25)
            gw_err = 0.5

        lines.append(f'clearing_http_request_duration_p99_ms{{service="risk-engine"}} {risk_p99:.2f} {ts}')
        lines.append(f'clearing_http_request_duration_p99_ms{{service="settlement-worker"}} {settle_p99:.2f} {ts}')
        lines.append(f'clearing_http_request_duration_p99_ms{{service="api-gateway"}} {gw_p99:.2f} {ts}')

        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-01",role="canary"}} {n1_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-02",role="worker"}} {n2_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-03",role="worker"}} {n3_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="settle-node-01",role="primary"}} {settle_p99:.2f} {ts}')

        lines.append(f'clearing_http_error_rate_percent{{service="risk-engine"}} {risk_err:.2f} {ts}')
        lines.append(f'clearing_http_error_rate_percent{{service="settlement-worker"}} {settle_err:.2f} {ts}')
        lines.append(f'clearing_http_error_rate_percent{{service="api-gateway"}} {gw_err:.2f} {ts}')

        lines.append(f'clearing_mutex_contention_wait_ms{{service="risk-engine",span="acquire_rule_lock"}} {mutex_wait:.2f} {ts}')
        lines.append(f'clearing_pg_active_connections{{cluster="pg-primary-01"}} {pg_conns} {ts}')

        lines.append(f'clearing_deploy_event{{service="api-gateway",version="v3.1.0"}} {1 if ts == t_benign_deploy else 0} {ts}')
        lines.append(f'clearing_deploy_event{{service="risk-engine",version="v2.8.4"}} {1 if ts == t_deploy else 0} {ts}')

    lines.append("# EOF")
    prom_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_logs_and_batches(data_dir: pathlib.Path) -> dict:
    """Generate gateway_requests.jsonl and settlement_batches_raw.csv and compute truth
    including MCH_VIP_003 Clause 4.2 (00:00-00:59 UTC+8 maintenance exemption)!
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261124)

    batch_specs = [
        ("BATCH-20261124-01", "MCH_VIP_001", "USD", 40, (800, 2200), "vip_mixed"),
        ("BATCH-20261124-02", "MCH_VIP_002", "CNY", 45, (7500, 14000), "vip_over_cap"),
        ("BATCH-20261124-03", "MCH_STD_010", "HKD", 35, (2000, 6500), "std_mixed"),
        ("BATCH-20261124-04", "MCH_VIP_003", "USD", 30, (600, 1800), "vip_tz_edge"),
    ]

    csv_rows = []
    log_entries = []
    tx_truth = {}

    tx_seq = 1
    for batch_id, merchant_id, currency, n_tx, (amin, amax), mode in batch_specs:
        tier = MERCHANTS[merchant_id]["tier"]
        for i in range(n_tx):
            tx_id = f"TX-20261124-{tx_seq:04d}"
            tx_seq += 1
            amt_orig = Decimal(str(round(rng.uniform(amin, amax), 2)))

            is_maint_exempt = False
            if mode == "vip_tz_edge" and i < 3:
                # 2026-11-23T16:30:00Z -> 2026-11-24T00:30:00+08:00 (counts in settled_amount_cny, EXEMPT from SLA breach!)
                base_dt = dt.datetime(2026, 11, 24, 0, 30 + i, 0, tzinfo=TZ_UTC8)
                ts_str = base_dt.astimezone(TZ_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                is_maint_exempt = True
            else:
                minute_off = rng.randint(0, 140)
                base_dt = dt.datetime(2026, 11, 24, 14, 14, 0, tzinfo=TZ_UTC8) + dt.timedelta(minutes=minute_off, seconds=rng.randint(0, 59))
                ts_str = base_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00") if i % 2 == 0 else base_dt.astimezone(TZ_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

            csv_rows.append({
                "batch_id": batch_id,
                "tx_id": tx_id,
                "merchant_id": merchant_id,
                "tier": tier,
                "currency": currency,
                "amount_orig": f"{amt_orig:.2f}",
                "cycle_time": base_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            })

            is_dup_risk = (i in (2, 11)) if mode in ("vip_mixed", "std_mixed") else (i == 5 and mode == "vip_tz_edge")
            has_normal_retry = (i in (4, 15, 22)) and not is_dup_risk

            if mode == "vip_over_cap":
                final_status = "504" if i % 3 == 0 else "SETTLED"
                final_lat = rng.choice([720, 880, 1250, 2100]) if i < 28 else rng.choice([250, 420])
            elif mode == "vip_mixed":
                if i < 6:
                    final_status, final_lat = "SETTLED", rng.randint(650, 950)
                elif i < 12:
                    final_status, final_lat = "TIMEOUT", 2400
                elif i < 16:
                    final_status, final_lat = "SETTLED", rng.randint(1100, 1600)
                else:
                    final_status, final_lat = "SETTLED", rng.randint(180, 560)
            elif mode == "std_mixed":
                if i < 8:
                    final_status, final_lat = "SETTLED", rng.randint(1550, 2100)
                elif i < 12:
                    final_status, final_lat = "504", 2600
                elif i < 18:
                    final_status, final_lat = "SETTLED", rng.randint(700, 1450)
                else:
                    final_status, final_lat = "SETTLED", rng.randint(150, 600)
            else:
                if i < 10:
                    # Note: i=0,1,2 are in 00:30-00:32 UTC+8 (high latency 850ms, but EXEMPT per Clause 4.2!)
                    final_status, final_lat = "SETTLED", rng.randint(640, 960)
                elif i < 14:
                    final_status, final_lat = "FAILED", 1900
                else:
                    final_status, final_lat = "SETTLED", rng.randint(200, 550)

            if is_dup_risk:
                t1 = base_dt
                t2 = base_dt + dt.timedelta(seconds=15)
                log_entries.append({
                    "timestamp": t1.astimezone(TZ_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "tx_id": tx_id, "merchant_id": merchant_id, "service": "api-gateway",
                    "status": "SETTLED", "latency_ms": final_lat, "attempt": 1,
                })
                log_entries.append({
                    "timestamp": t2.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "tx_id": tx_id, "merchant_id": merchant_id, "service": "api-gateway",
                    "status": final_status, "latency_ms": final_lat, "attempt": 2,
                })
            elif has_normal_retry:
                t1 = base_dt - dt.timedelta(seconds=12)
                log_entries.append({
                    "timestamp": t1.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "tx_id": tx_id, "merchant_id": merchant_id, "service": "api-gateway",
                    "status": "TIMEOUT", "latency_ms": 2500, "attempt": 1,
                })
                log_entries.append({
                    "timestamp": ts_str,
                    "tx_id": tx_id, "merchant_id": merchant_id, "service": "api-gateway",
                    "status": final_status, "latency_ms": final_lat, "attempt": 2,
                })
            else:
                log_entries.append({
                    "timestamp": ts_str,
                    "tx_id": tx_id, "merchant_id": merchant_id, "service": "api-gateway",
                    "status": final_status, "latency_ms": final_lat, "attempt": 1,
                })

            tx_truth[tx_id] = {
                "batch_id": batch_id,
                "merchant_id": merchant_id,
                "tier": tier,
                "currency": currency,
                "amount_orig": amt_orig,
                "final_status": final_status,
                "final_latency_ms": final_lat,
                "is_dup_risk": is_dup_risk,
                "is_maint_exempt": is_maint_exempt,
            }

    for k in range(25):
        t_test = dt.datetime(2026, 11, 24, 14, 20, k % 59, tzinfo=TZ_UTC8)
        log_entries.append({
            "timestamp": t_test.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "tx_id": f"TX-TEST-{k:04d}",
            "merchant_id": "TEST_SANDBOX_01" if k % 2 == 0 else "TEST_QA_99",
            "service": "api-gateway", "status": "504", "latency_ms": 4200, "attempt": 1,
        })
    for k in range(6):
        t_next = dt.datetime(2026, 11, 24, 16, 30 + k, 0, tzinfo=TZ_UTC)
        log_entries.append({
            "timestamp": t_next.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tx_id": f"TX-20261125-NEXTDAY-{k:02d}",
            "merchant_id": "MCH_VIP_003", "service": "api-gateway",
            "status": "504", "latency_ms": 3800, "attempt": 1,
        })
    for k in range(30):
        t_r = dt.datetime(2026, 11, 24, 14, 14, k % 59, tzinfo=TZ_UTC8)
        log_entries.append({
            "timestamp": t_r.astimezone(TZ_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tx_id": f"INTERNAL-RISK-PROBE-{k:03d}",
            "merchant_id": "TEST_INTERNAL_PROBE", "service": "risk-engine",
            "status": "SLOW_LOCK", "latency_ms": 1480 + k * 10, "attempt": 1, "release": "v2.8.4",
        })

    rng.shuffle(log_entries)
    with (data_dir / "gateway_requests.jsonl").open("w", encoding="utf-8") as f:
        for entry in log_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    with (data_dir / "settlement_batches_raw.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["batch_id", "tx_id", "merchant_id", "tier", "currency", "amount_orig", "cycle_time"])
        writer.writeheader()
        writer.writerows(csv_rows)

    batch_results = {}
    dup_risk_tx_ids = sorted([tx_id for tx_id, info in tx_truth.items() if info["is_dup_risk"]])

    for batch_id, merchant_id, currency, *_ in batch_specs:
        tier = MERCHANTS[merchant_id]["tier"]
        threshold = 600 if tier == "VIP_T1" else 1500
        rate = Decimal("0.025") if tier == "VIP_T1" else Decimal("0.005")
        cap = Decimal("5000.00") if tier == "VIP_T1" else None
        fx = FX_RATES[currency]

        b_txs = [v for v in tx_truth.values() if v["batch_id"] == batch_id]
        total_cny = sum(q2(v["amount_orig"] * fx) for v in b_txs)
        breached = [
            v for v in b_txs
            if (not v["is_maint_exempt"])
            and (v["final_status"] in ("TIMEOUT", "504", "FAILED") or v["final_latency_ms"] > threshold)
        ]
        dup_cnt = sum(1 for v in b_txs if v["is_dup_risk"])
        raw_pen = sum(q2(v["amount_orig"] * fx) * rate for v in breached)
        pen = q2(raw_pen)
        if cap is not None and pen > cap:
            pen = cap

        batch_results[batch_id] = {
            "batch_id": batch_id,
            "merchant_id": merchant_id,
            "tier": tier,
            "currency": currency,
            "settled_amount_cny": float(q2(total_cny)),
            "breached_tx_count": len(breached),
            "duplicate_risk_count": dup_cnt,
            "penalty_cny": float(pen),
        }

    total_pen = q2(sum(Decimal(str(b["penalty_cny"])) for b in batch_results.values()))
    return {
        "root_cause_service": "risk-engine",
        "onset_time_utc8": "2026-11-24T14:14:00+08:00",
        "culprit_version": "v2.8.4",
        "bottleneck_span": "acquire_rule_lock",
        "isolated_nodes": ["risk-node-02", "risk-node-03"],
        "active_nodes": ["risk-node-01", "settle-node-01", "settle-node-02", "pg-primary-01"],
        "freeze_window_utc8": ["17:30", "19:30"],
        "batches": batch_results,
        "total_penalty_cny": float(total_pen),
        "duplicate_risk_tx_ids": dup_risk_tx_ids,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-dir", required=True)
    ap.add_argument("--seed-dir", required=True)
    ap.add_argument("--truth-path", required=True)
    ap.add_argument("--trace-dir", default="/opt/services/traces",
                    help="Root-only trace directory (agent must NOT read trace files directly)")
    args = ap.parse_args()

    ws = pathlib.Path(args.workspace_dir)
    sd = pathlib.Path(args.seed_dir)
    trace_dir = pathlib.Path(args.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    generate_visual_and_contract_docs(sd, ws / "contracts")
    generate_complex_pftrace(trace_dir / "clearing_incident.pftrace")
    generate_openmetrics(sd / "metrics.openmetrics")
    truth = generate_logs_and_batches(ws / "data")
    pathlib.Path(args.truth_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.truth_path).write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    print("v2 generated! Total penalty CNY:", truth["total_penalty_cny"])
    print("Batches:", json.dumps(truth["batches"], ensure_ascii=False))
    print("Dup risk tx_ids:", truth["duplicate_risk_tx_ids"])


if __name__ == "__main__":
    main()
