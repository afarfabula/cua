#!/usr/bin/env python3
"""v0.5.0 Deterministic generator:
- Binary Perfetto .pftrace (6 processes, 28 threads, 10,000+ noisy realistic slices, ZERO spoiler strings)
- Multi-resolution 5s telemetry series for server-side zoomable chart PNGs
- Multi-noise Prometheus OpenMetrics
- True Multimodal Mailpit attachments:
  * Non-spoiler DBA pg-primary-01 connection & lock chart PNG
  * Two-image visual SLA decoding: sla_corridor_matrix.png (no rule text) + sla_legend_appendix.png
  * Pure-raster scanned PDFs (ZERO extractable text layer) with red-pen strikethrough & annotations
- Coupled SLA ground truth (Mutex Highest-Only + Clause 4.2 maintenance base coupling)
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import pathlib
import random
from decimal import Decimal, ROUND_HALF_UP

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
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


def _save_raster_pdf(img: Image.Image, out_path: pathlib.Path) -> None:
    """Embed a raster PIL Image into a PDF page with zero extractable text layer."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    c.drawImage(ImageReader(buf), 0, 0, width=w, height=h)
    c.showPage()
    c.save()


def generate_visual_and_contract_docs(seed_dir: pathlib.Path, contracts_dir: pathlib.Path) -> None:
    seed_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    font_h = ImageFont.load_default(size=22)
    font_b = ImageFont.load_default(size=17)
    font_s = ImageFont.load_default(size=14)
    font_xs = ImageFont.load_default(size=12)

    # 1. DBA PostgreSQL Connection Pool & Lock-Wait Chart (pure visual chart, ZERO PR verdict spoilers)
    img1 = Image.new("RGB", (1040, 620), (248, 250, 252))
    d1 = ImageDraw.Draw(img1)
    d1.rectangle([20, 20, 1020, 82], fill=(15, 23, 42))
    d1.text((40, 40), "pg-primary-01 Capacity & Lock Telemetry (2026-11-24 14:00-15:00 UTC+8)", fill=(255, 255, 255), font=font_h)

    px1, py1, px2, py2 = 90, 115, 980, 340
    d1.rectangle([px1, py1, px2, py2], fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    d1.text((px1 + 12, py1 + 8), "Active Connections vs Cluster Hard Limit", fill=(15, 23, 42), font=font_b)
    for y_val in (0, 60, 120, 180, 220):
        yy = py2 - 25 - int((y_val / 240.0) * (py2 - py1 - 55))
        d1.line([(px1 + 45, yy), (px2 - 20, yy)], fill=(226, 232, 240), width=1)
        d1.text((px1 + 6, yy - 7), str(y_val), fill=(100, 116, 139), font=font_xs)
    y_180 = py2 - 25 - int((180 / 240.0) * (py2 - py1 - 55))
    for x in range(px1 + 45, px2 - 20, 18):
        d1.line([(x, y_180), (min(x + 10, px2 - 20), y_180)], fill=(220, 38, 38), width=3)
    d1.text((px1 + 56, y_180 - 22), "pg_max_connections = 180 (HARD LIMIT)", fill=(185, 28, 28), font=font_s)

    conn_pts = [42, 43, 41, 44, 58, 92, 128, 152, 168, 166, 164, 161, 158]
    plot_pts = []
    for idx, val in enumerate(conn_pts):
        xx = px1 + 55 + int(idx * (px2 - px1 - 95) / (len(conn_pts) - 1))
        yy = py2 - 25 - int((val / 240.0) * (py2 - py1 - 55))
        plot_pts.append((xx, yy))
        if idx % 2 == 0:
            d1.text((xx - 15, py2 - 20), f"14:{idx*5:02d}", fill=(100, 116, 139), font=font_xs)
    d1.line(plot_pts, fill=(37, 99, 235), width=3)
    for xx, yy in plot_pts:
        d1.ellipse([xx - 4, yy - 4, xx + 4, yy + 4], fill=(37, 99, 235))
    d1.text((plot_pts[8][0] - 42, plot_pts[8][1] - 22), "peak=168 (93%)", fill=(30, 64, 175), font=font_xs)

    bx1, by1, bx2, by2 = 90, 365, 980, 585
    d1.rectangle([bx1, by1, bx2, by2], fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    d1.text((bx1 + 12, by1 + 10), "Session Wait Breakdown at 14:20 UTC+8 (Count of Sessions)", fill=(15, 23, 42), font=font_b)
    bars = [
        ("risk-engine: waiting_on_row_lock", 44, (220, 38, 38)),
        ("risk-engine: idle_in_transaction", 39, (234, 88, 12)),
        ("settlement-worker: pool_wait", 18, (217, 119, 6)),
        ("api-gateway: active_read", 8, (22, 163, 74)),
    ]
    for i, (label, val, col) in enumerate(bars):
        ry = by1 + 52 + i * 38
        d1.text((bx1 + 20, ry + 4), label, fill=(30, 41, 59), font=font_s)
        bar_left = bx1 + 360
        bar_w = int((val / 50.0) * 440)
        d1.rectangle([bar_left, ry, bar_left + bar_w, ry + 24], fill=col)
        d1.text((bar_left + bar_w + 10, ry + 4), f"{val} sessions", fill=col, font=font_s)
    img1.save(seed_dir / "dba_pg_lock_graph.png")

    # 2A. Visual SLA Corridor Matrix PNG (NO rule/rate/cap/exemption text — strictly visual encoding!)
    img_mat = Image.new("RGB", (1040, 560), (255, 255, 255))
    dm = ImageDraw.Draw(img_mat)
    dm.rectangle([20, 20, 1020, 82], fill=(15, 23, 42))
    dm.text((40, 40), "SLA CORRIDOR MATRIX (2026-11-24) — DECODE WITH LEGEND APPENDIX", fill=(255, 255, 255), font=font_h)

    cards = [
        (40, 110, 500, 305, (254, 226, 226), (220, 38, 38), "MCH_VIP_001", "Currency: USD  |  Batch: BATCH-20261124-01", True, False, True),
        (540, 110, 1000, 305, (254, 226, 226), (220, 38, 38), "MCH_VIP_002", "Currency: CNY  |  Batch: BATCH-20261124-02", True, False, False),
        (40, 330, 500, 525, (220, 252, 231), (22, 163, 74), "MCH_STD_010", "Currency: HKD  |  Batch: BATCH-20261124-03", False, False, True),
        (540, 330, 1000, 525, (254, 226, 226), (220, 38, 38), "MCH_VIP_003", "Currency: USD  |  Batch: BATCH-20261124-04", True, True, True),
    ]
    for x1, y1, x2, y2, bg, bd, title, sub, tri, hatch, dbl in cards:
        if dbl:
            dm.rectangle([x1 - 6, y1 - 6, x2 + 6, y2 + 6], outline=(30, 58, 138), width=4)
            dm.rectangle([x1, y1, x2, y2], fill=bg, outline=(30, 58, 138), width=3)
        else:
            dm.rectangle([x1, y1, x2, y2], fill=bg, outline=bd, width=4)
        dm.text((x1 + 22, y1 + 26), title, fill=(15, 23, 42), font=font_h)
        dm.text((x1 + 22, y1 + 72), sub, fill=(30, 41, 59), font=font_b)
        if tri:
            dm.polygon([(x2 - 48, y1 + 48), (x2 - 18, y1 + 48), (x2 - 33, y1 + 18)], fill=(220, 38, 38))
        if hatch:
            hy1, hy2 = y2 - 42, y2 - 6
            dm.rectangle([x1 + 6, hy1, x2 - 6, hy2], fill=(254, 243, 199), outline=(217, 119, 6), width=2)
            for hx in range(x1 + 10, x2 - 14, 18):
                dm.line([(hx, hy2), (hx + 16, hy1)], fill=(217, 119, 6), width=4)
    img_mat.save(seed_dir / "sla_corridor_matrix.png")

    # 2B. Visual Legend Appendix PNG
    img_leg = Image.new("RGB", (1040, 640), (255, 253, 247))
    dl = ImageDraw.Draw(img_leg)
    dl.rectangle([20, 20, 1020, 82], fill=(30, 41, 59))
    dl.text((40, 40), "SLA VISUAL LEGEND APPENDIX (COMP-ERRATA-20261124-REV2)", fill=(255, 255, 255), font=font_h)

    rows = [
        ("swatch_red", "Crimson Red Card = VIP_T1: Threshold > 600 ms (or TIMEOUT/504/FAILED) -> SLA 2.5% | Dup-Debit Comp 3.0%"),
        ("swatch_green", "Emerald Green Card = STD: Threshold > 1500 ms (or TIMEOUT/504/FAILED) -> SLA 0.5% | Dup-Debit Comp 1.0%"),
        ("triangle", "Top-Right Red Triangle = Per-Batch Penalty Cap: min(batch_raw_penalty, 5000.00 CNY)"),
        ("hatch", "Bottom Amber Diagonal Hatch = Clause 4.2 Maint Window Exempt (00:00-01:00 UTC+8): 0 breach, counts in settled_cny"),
        ("double", "Double Navy Outer Border = Mutex Highest-Only: tx_penalty = max(sla_penalty, dup_comp) (no stacking)"),
    ]
    for i, (kind, desc) in enumerate(rows):
        ry = 115 + i * 74
        dl.rectangle([40, ry, 1000, ry + 58], fill=(255, 255, 255), outline=(203, 213, 225), width=2)
        if kind == "swatch_red":
            dl.rectangle([56, ry + 10, 106, ry + 48], fill=(254, 226, 226), outline=(220, 38, 38), width=3)
        elif kind == "swatch_green":
            dl.rectangle([56, ry + 10, 106, ry + 48], fill=(220, 252, 231), outline=(22, 163, 74), width=3)
        elif kind == "triangle":
            dl.polygon([(62, ry + 46), (100, ry + 46), (81, ry + 12)], fill=(220, 38, 38))
        elif kind == "hatch":
            dl.rectangle([54, ry + 12, 108, ry + 46], fill=(254, 243, 199), outline=(217, 119, 6), width=2)
            for hx in range(58, 104, 12):
                dl.line([(hx, ry + 45), (hx + 10, ry + 13)], fill=(217, 119, 6), width=3)
        elif kind == "double":
            dl.rectangle([52, ry + 8, 110, ry + 50], outline=(30, 58, 138), width=3)
            dl.rectangle([58, ry + 14, 104, ry + 44], outline=(30, 58, 138), width=2)
        dl.text((126, ry + 20), desc, fill=(15, 23, 42), font=font_s)

    dl.rectangle([40, 495, 1000, 608], fill=(241, 245, 249), outline=(148, 163, 184), width=2)
    dl.text((56, 512), "2026-11-24 Settlement FX:  1 USD = 7.15 CNY   |   1 HKD = 0.92 CNY   |   1 CNY = 1.00 CNY", fill=(15, 23, 42), font=font_b)
    dl.text((56, 545), "Rounding: tx_cny = round_half_up(amount_orig * fx, 2); batch_pen = min(cap, round_half_up(sum(tx_pen), 2))", fill=(51, 65, 85), font=font_s)
    dl.text((56, 574), "Initial EOD Change Freeze Window: 17:30 - 19:30 UTC+8 (subject to live CAB extension notices)", fill=(153, 27, 27), font=font_s)
    img_leg.save(seed_dir / "sla_legend_appendix.png")

    # 3. Pure-Raster Scanned PDF: REV-2 Errata with red strikethrough & handwritten annotations (NO PDF text layer)
    img_err = Image.new("RGB", (1240, 1754), (255, 253, 246))
    de = ImageDraw.Draw(img_err)
    de.rectangle([45, 45, 1195, 1705], outline=(127, 29, 29), width=5)
    de.rectangle([60, 60, 1180, 165], fill=(153, 27, 27))
    de.text((90, 96), "COMPLIANCE & CAB URGENT ERRATA NOTICE (REV-2)", fill=(255, 255, 255), font=ImageFont.load_default(size=30))
    de.text((80, 195), "Document ID: COMP-ERRATA-20261124-REV2   |   Effective: 2026-11-24 00:00:00 +08:00", fill=(30, 41, 59), font=ImageFont.load_default(size=22))
    de.text((80, 235), "Supersedes sla_policy_v1.md and unsigned discussion draft v1.5.", fill=(71, 85, 105), font=ImageFont.load_default(size=20))

    f_print = ImageFont.load_default(size=22)
    f_red = ImageFont.load_default(size=23)
    amendments = [
        ("1. VIP_T1 SLA Latency Threshold:", "1000 ms", "-> REV-2: 600 ms (strictly >)"),
        ("2. VIP_T1 SLA Breach Rate:", "1.5% (0.015)", "-> REV-2: 2.5% (0.025)"),
        ("3. VIP_T1 Per-Batch Penalty Cap:", "Uncapped", "-> REV-2: Cap 5000.00 CNY"),
        ("4. Daily EOD Change Freeze Window:", "20:00 - 22:00 UTC+8", "-> REV-2: 17:30 - 19:30 UTC+8"),
    ]
    yy = 320
    for title, old_val, new_val in amendments:
        de.text((80, yy), title, fill=(15, 23, 42), font=f_print)
        de.text((510, yy), old_val, fill=(100, 116, 139), font=f_print)
        old_w = len(old_val) * 13
        de.line([(505, yy + 14), (515 + old_w, yy + 14)], fill=(220, 38, 38), width=4)
        de.text((535 + old_w, yy - 2), new_val, fill=(185, 28, 28), font=f_red)
        yy += 78

    extra_lines = [
        "5. Standard Tier (STD): 1500 ms threshold, 0.5% (0.005) SLA rate, uncapped.",
        "6. Duplicate-Debit Risk Compensation (DUPLICATE_DEBIT_RISK):",
        "   - VIP_T1: 3.0% (0.030) of tx_cny  |  STD: 1.0% (0.010) of tx_cny",
        "   - For double-bordered corridors on visual matrix: Mutex Highest-Only",
        "     tx_penalty = max(sla_penalty, dup_compensation) (NEVER stack both).",
        "7. Settlement FX (2026-11-24): 1 USD = 7.15 CNY | 1 HKD = 0.92 CNY | 1 CNY = 1.00 CNY",
        "8. Rounding: tx_cny = round_half_up(amount_orig * fx, 2); batch_pen = min(cap, round_half_up(sum, 2)).",
        "9. Sandbox Filter: Exclude TEST_ merchants; convert all timestamps to UTC+8 before 2026-11-24 filter.",
        "10. Final Approval Gate: Follow live CAB review extensions emitted during change approval.",
    ]
    yy += 30
    for line in extra_lines:
        de.text((80, yy), line, fill=(15, 23, 42), font=f_print)
        yy += 52

    de.ellipse([860, 1280, 1140, 1520], outline=(220, 38, 38), width=7)
    de.ellipse([872, 1292, 1128, 1508], outline=(220, 38, 38), width=3)
    de.text((915, 1360), "APPROVED SEAL", fill=(220, 38, 38), font=ImageFont.load_default(size=24))
    de.text((905, 1400), "RISK & COMPLIANCE", fill=(220, 38, 38), font=ImageFont.load_default(size=19))
    de.text((940, 1436), "2026-11-24", fill=(220, 38, 38), font=ImageFont.load_default(size=22))
    _save_raster_pdf(img_err, seed_dir / "compliance_errata_20261124.pdf")

    # 4. Distractor VOID Draft Scan PDF (pure raster, unsealed)
    img_void = Image.new("RGB", (1240, 1754), (250, 250, 250))
    dv = ImageDraw.Draw(img_void)
    dv.text((80, 120), "INTERNAL DISCUSSION DRAFT v1.5 (NOT APPROVED)", fill=(30, 41, 59), font=ImageFont.load_default(size=30))
    for idx, line in enumerate([
        "Proposed VIP_T1 Threshold: 800 ms | Penalty Rate: 2.0% | Cap: 4000.00 CNY",
        "Proposed EOD Freeze Window: 19:00 - 21:00 UTC+8",
        "Proposed Duplicate-Debit Stacking: Add SLA + Duplicate penalties together",
        "NOTE: Unsigned draft superseded by COMP-ERRATA-20261124-REV2.",
    ]):
        dv.text((80, 230 + idx * 65), line, fill=(51, 65, 85), font=f_print)
    wm = Image.new("RGBA", (1240, 1754), (0, 0, 0, 0))
    dw = ImageDraw.Draw(wm)
    dw.text((180, 820), "DRAFT - VOID / SUPERSEDED", fill=(180, 180, 180, 160), font=ImageFont.load_default(size=60))
    wm = wm.rotate(30)
    img_void = Image.alpha_composite(img_void.convert("RGBA"), wm).convert("RGB")
    _save_raster_pdf(img_void, seed_dir / "sla_draft_v1_5_void.pdf")

    # 5. Pure-Raster Contract Scan PDF (vip_merchant_framework_2026.pdf)
    img_con = Image.new("RGB", (1240, 1754), (255, 255, 255))
    dc = ImageDraw.Draw(img_con)
    dc.rectangle([50, 50, 1190, 1700], outline=(30, 41, 59), width=3)
    dc.text((80, 100), "VIP MERCHANT FRAMEWORK AGREEMENT (2026)", fill=(15, 23, 42), font=ImageFont.load_default(size=28))
    contract_lines = [
        "Contract Reference: FIN-MSA-2026-VIP-088",
        "",
        "Clause 4.1: Standard SLA remedies apply unless superseded by a sealed regulatory errata notice.",
        "Clause 4.2 (Scheduled Maintenance Exemption for MCH_VIP_003):",
        "  Transactions settled for Meridian Luxury Pay (MCH_VIP_003) between",
        "  00:00:00 and 00:59:59 UTC+8 (16:00:00-16:59:59 UTC prior day)",
        "  are EXEMPT from SLA latency breach counts and SLA breach penalties,",
        "  while remaining INCLUDED in settled_amount_cny batch volume.",
        "Clause 5.1 (Mutex Highest-Only Non-Stacking):",
        "  For corridors designated with mutex protection, if a single transaction",
        "  incurs both an SLA breach and a DUPLICATE_DEBIT_RISK event, total",
        "  compensation for that transaction equals max(sla_penalty, dup_comp).",
        "Clause 6.1: All batch audits are evaluated strictly in UTC+8 calendar day.",
    ]
    cy = 200
    for line in contract_lines:
        dc.text((80, cy), line, fill=(15, 23, 42), font=f_print)
        cy += 58
    _save_raster_pdf(img_con, contracts_dir / "vip_merchant_framework_2026.pdf")


def generate_complex_pftrace(trace_path: pathlib.Path) -> None:
    """Generate authentic binary .pftrace with 6 processes, 28 threads, 10,000+ slices."""
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    w = PftraceWriter(seq_id=2026)

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
    t0_ns = 1_000_000_000

    for sec in range(45):
        base = t0_ns + sec * 1_000_000_000
        for sub in range(4):
            ts = base + sub * 250_000_000
            w.add_slice(threads["gw-healthcheck"], ts, 1_200_000, "http.get(/healthz)", "net")
            w.add_slice(threads["redis-heartbeat-0"], ts + 5_000_000, 800_000, "redis.PING", "cache")
            w.add_slice(threads["redis-heartbeat-1"], ts + 12_000_000, 900_000, "sentinel.info", "cache")
            w.add_slice(threads["settle-pg-keepalive"], ts + 20_000_000, 2_100_000, "pg.SELECT_1", "db")
            w.add_slice(threads["fx-cache-warmer"], ts + 30_000_000, 3_500_000, "fx.cache_stat", "cache")
        w.add_slice(threads["gw-metrics-flush"], base + 100_000_000, 8_000_000, "prometheus.scrape_collect", "telemetry")
        w.add_slice(threads["risk-metrics-pusher"], base + 150_000_000, 9_500_000, "metrics.export_batch", "telemetry")
        w.add_slice(threads["risk-rule-sync"], base + 200_000_000, 12_000_000, "rules.poll_snapshot", "config")
        if sec % 3 == 0:
            w.add_slice(threads["risk-gc-daemon"], base + 400_000_000, 18_000_000, "gc.minor_sweep", "runtime")
        if sec % 5 == 0:
            w.add_slice(threads["settle-recon-cron"], base + 500_000_000, 42_000_000, "recon.checkpoint_scan", "batch")

        if sec < 15 or sec >= 36:
            for k in range(35):
                ts = base + k * 27_000_000 + rng.randint(0, 4_000_000)
                w.add_slice(threads[f"gw-http-worker-{k % 4}"], ts, 14_000_000, "GatewayHandler::handle", "http", {"status": 200})
                w.add_slice(threads[f"risk-worker-{k % 8}"], ts + 2_000_000, 6_000_000, "RiskEngine::evaluate_tx", "risk", {"pair": "USD_CNY"})
                w.add_slice(threads[f"risk-worker-{k % 8}"], ts + 2_500_000, 1_200_000, "RiskEngine::get_fx_rate", "risk")
                w.add_slice(threads[f"settle-dispatcher-{k % 6}"], ts + 9_000_000, 4_500_000, "SettlementDispatcher::submit", "settlement")
                w.add_slice(threads[f"pgb-pool-{k % 3}"], ts + 10_000_000, 2_500_000, "pg.INSERT_settlement", "db")

    lock_free_ns = t0_ns + 15 * 1_000_000_000
    for idx in range(42):
        arr_ns = t0_ns + 15 * 1_000_000_000 + idx * 45_000_000
        rw_t = threads[f"risk-worker-{idx % 8}"]
        gw_t = threads[f"gw-http-worker-{idx % 4}"]
        sw_t = threads[f"settle-dispatcher-{idx % 6}"]
        fx_t = threads[f"fx-handler-{idx % 3}"]
        pg_t = threads[f"pgb-pool-{idx % 3}"]

        start_hold = max(arr_ns, lock_free_ns)
        wait_ns = start_hold - arr_ns
        hold_ns = 480_000_000 + rng.randint(10_000_000, 75_000_000)
        lock_free_ns = start_hold + hold_ns

        w.add_slice(gw_t, arr_ns, wait_ns + hold_ns + 25_000_000, "GatewayHandler::handle", "http", {"status": 504})
        w.add_slice(rw_t, arr_ns, wait_ns + hold_ns, "RiskEngine::evaluate_tx", "risk", {"pair": "HKD_CNY" if idx % 2 else "USD_CNY"})
        if wait_ns > 0:
            w.add_slice(rw_t, arr_ns, wait_ns, "Mutex::wait(_rule_cache_lock)", "lock", {"contended": True})
        w.add_slice(rw_t, start_hold, hold_ns, "acquire_rule_lock", "lock", {"lock": "_rule_cache_lock", "blocking_io": True})
        w.add_slice(rw_t, start_hold + 2_000_000, hold_ns - 4_000_000, "fx_quote_http_retry", "net", {"http_status": 503, "retries": 3})
        w.add_slice(fx_t, start_hold + 4_000_000, hold_ns - 8_000_000, "FxUpstream::quote_timeout", "net", {"upstream": "fx-provider"})
        w.add_slice(sw_t, arr_ns + 200_000_000, 320_000_000, "SettlementDispatcher::retry_storm", "settlement", {"attempt": 2})
        w.add_slice(pg_t, arr_ns + 210_000_000, 290_000_000, "pg.LOCK_WAIT_idempotency", "db", {"active_conns": 168})

    trace_path.write_bytes(w.to_bytes())


def generate_telemetry_series(out_path: pathlib.Path) -> None:
    """Generate 5-second resolution telemetry (14:00-18:00 UTC+8) for Portal drill-down PNGs."""
    start_dt = dt.datetime(2026, 11, 24, 14, 0, 0, tzinfo=TZ_UTC8)
    end_dt = dt.datetime(2026, 11, 24, 18, 0, 0, tzinfo=TZ_UTC8)
    t_r2 = dt.datetime(2026, 11, 24, 14, 13, 25, tzinfo=TZ_UTC8)
    t_r3 = dt.datetime(2026, 11, 24, 14, 13, 40, tzinfo=TZ_UTC8)
    t_s1 = dt.datetime(2026, 11, 24, 14, 14, 50, tzinfo=TZ_UTC8)
    t_r4_s = dt.datetime(2026, 11, 24, 14, 15, 15, tzinfo=TZ_UTC8)
    t_r4_e = dt.datetime(2026, 11, 24, 14, 15, 40, tzinfo=TZ_UTC8)
    t_mit = dt.datetime(2026, 11, 24, 16, 45, 0, tzinfo=TZ_UTC8)

    rng = random.Random(2026112405)
    timestamps = []
    series = {k: [] for k in [
        "risk-node-01", "risk-node-02", "risk-node-03", "risk-node-04",
        "risk-node-05", "settle-node-01", "settle-node-02", "gw-edge-01",
    ]}

    cur = start_dt
    while cur <= end_dt:
        timestamps.append(cur.strftime("%H:%M:%S"))
        series["risk-node-01"].append(round(84 + rng.uniform(-5, 6), 1))
        series["risk-node-05"].append(round(88 + rng.uniform(-5, 6), 1))
        series["settle-node-02"].append(round(94 + rng.uniform(-6, 7), 1))

        if cur < t_r2:
            v2 = 86 + rng.uniform(-5, 6)
        elif cur < t_mit:
            sec_since = (cur - t_r2).total_seconds()
            burst = 2300.0 * math.exp(-((sec_since - 45.0) / 35.0) ** 2)
            v2 = 1520.0 + burst + rng.uniform(-45, 55)
        else:
            v2 = 245 + rng.uniform(-15, 20)
        series["risk-node-02"].append(round(v2, 1))

        if cur < t_r3:
            v3 = 85 + rng.uniform(-5, 6)
        elif cur < t_mit:
            sec_since = (cur - t_r3).total_seconds()
            burst = 2580.0 * math.exp(-((sec_since - 40.0) / 32.0) ** 2)
            v3 = 1480.0 + burst + rng.uniform(-45, 55)
        else:
            v3 = 238 + rng.uniform(-15, 20)
        series["risk-node-03"].append(round(v3, 1))

        if cur < t_s1:
            vs1 = 108 + rng.uniform(-8, 10)
        elif cur < t_mit:
            ramp = min(1.0, (cur - t_s1).total_seconds() / 180.0)
            vs1 = 150 + ramp * 1240 + rng.uniform(-35, 45)
        else:
            vs1 = 185 + rng.uniform(-15, 18)
        series["settle-node-01"].append(round(vs1, 1))

        if t_r4_s <= cur <= t_r4_e:
            v4 = 1820 + rng.uniform(-90, 140)
        elif t_r4_e < cur < t_r4_e + dt.timedelta(seconds=20):
            v4 = 420 + rng.uniform(-40, 40)
        else:
            v4 = 91 + rng.uniform(-5, 7)
        series["risk-node-04"].append(round(v4, 1))

        if cur < t_r4_s:
            vgw = 135 + rng.uniform(-8, 12)
        elif cur < t_mit:
            ramp = min(1.0, (cur - t_r4_s).total_seconds() / 240.0)
            vgw = 240 + ramp * 2100 + rng.uniform(-60, 80)
        else:
            vgw = 265 + rng.uniform(-15, 20)
        series["gw-edge-01"].append(round(vgw, 1))

        cur += dt.timedelta(seconds=5)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "start_utc8": "2026-11-24T14:00:00+08:00",
        "step_sec": 5,
        "timestamps": timestamps,
        "series": series,
    }), encoding="utf-8")


def generate_openmetrics(prom_file: pathlib.Path) -> None:
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
        if ts < t_risk_onset:
            risk_p99 = 85 + cron_noise * 0.2 + rng.uniform(-8, 10)
            n1_p99, n2_p99, n3_p99 = 82 + rng.uniform(-5, 6), 86 + rng.uniform(-5, 6), 84 + rng.uniform(-5, 6)
            n4_p99, n5_p99 = 91 + rng.uniform(-5, 6), 88 + rng.uniform(-5, 6)
            risk_err, mutex_wait = 0.1, 2.0 + rng.uniform(0, 1.5)
        elif ts < t_mitigate:
            ramp = min(1.0, (ts - t_risk_onset) / 300.0)
            risk_p99 = 120 + ramp * 1450 + rng.uniform(-40, 80)
            n1_p99 = 88 + rng.uniform(-6, 8)
            n2_p99 = 135 + ramp * 1510 + rng.uniform(-30, 60)
            n3_p99 = 130 + ramp * 1470 + rng.uniform(-30, 60)
            n4_p99 = 180.0 if ts == t_risk_onset + 60 else 92 + rng.uniform(-5, 6)
            n5_p99 = 89 + rng.uniform(-5, 6)
            risk_err = 1.5 + ramp * 14.0
            mutex_wait = 45 + ramp * 470 + rng.uniform(-15, 25)
        else:
            risk_p99, n1_p99, n2_p99, n3_p99 = 240 + rng.uniform(-20, 30), 85 + rng.uniform(-5, 6), 260 + rng.uniform(-20, 25), 250 + rng.uniform(-20, 25)
            n4_p99, n5_p99 = 91 + rng.uniform(-5, 6), 88 + rng.uniform(-5, 6)
            risk_err, mutex_wait = 0.8, 12 + rng.uniform(0, 5)

        if ts < t_settle_onset:
            settle_p99, settle_err, pg_conns = 110 + cron_noise + rng.uniform(-10, 12), 0.05, 42 + rng.randint(-3, 4)
        elif ts < t_mitigate:
            ramp = min(1.0, (ts - t_settle_onset) / 360.0)
            settle_p99, settle_err, pg_conns = 160 + ramp * 1680 + rng.uniform(-50, 90), 1.0 + ramp * 18.0, int(50 + ramp * 118)
        else:
            settle_p99, settle_err, pg_conns = 190 + rng.uniform(-15, 20), 0.4, 58 + rng.randint(-4, 4)

        if ts < t_gw_onset:
            gw_p99, gw_err = 140 + cron_noise * 0.4 + rng.uniform(-10, 15), 0.1
        elif ts < t_mitigate:
            ramp = min(1.0, (ts - t_gw_onset) / 240.0)
            gw_p99, gw_err = 250 + ramp * 2400 + rng.uniform(-80, 120), 2.0 + ramp * 34.0
        else:
            gw_p99, gw_err = 280 + rng.uniform(-20, 25), 0.5

        lines.append(f'clearing_http_request_duration_p99_ms{{service="risk-engine"}} {risk_p99:.2f} {ts}')
        lines.append(f'clearing_http_request_duration_p99_ms{{service="settlement-worker"}} {settle_p99:.2f} {ts}')
        lines.append(f'clearing_http_request_duration_p99_ms{{service="api-gateway"}} {gw_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-01",role="canary"}} {n1_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-02",role="worker"}} {n2_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-03",role="worker"}} {n3_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-04",role="worker"}} {n4_p99:.2f} {ts}')
        lines.append(f'clearing_node_p99_latency_ms{{node="risk-node-05",role="worker"}} {n5_p99:.2f} {ts}')
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
    including MCH_VIP_003 Clause 4.2 exemption AND Mutex Highest-Only non-stacking!
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
        sla_rate = Decimal("0.025") if tier == "VIP_T1" else Decimal("0.005")
        dup_rate = Decimal("0.030") if tier == "VIP_T1" else Decimal("0.010")
        cap = Decimal("5000.00") if tier == "VIP_T1" else None
        fx = FX_RATES[currency]

        b_txs = [v for v in tx_truth.values() if v["batch_id"] == batch_id]
        total_cny = Decimal("0.00")
        breached_cnt = 0
        dup_cnt = 0
        raw_pen = Decimal("0.00")

        for v in b_txs:
            tx_cny = q2(v["amount_orig"] * fx)
            total_cny += tx_cny
            is_breached = (not v["is_maint_exempt"]) and (
                v["final_status"] in ("TIMEOUT", "504", "FAILED") or v["final_latency_ms"] > threshold
            )
            if is_breached:
                breached_cnt += 1
            if v["is_dup_risk"]:
                dup_cnt += 1
            sla_pen = (tx_cny * sla_rate) if is_breached else Decimal("0.00")
            dup_comp = (tx_cny * dup_rate) if v["is_dup_risk"] else Decimal("0.00")
            raw_pen += max(sla_pen, dup_comp)

        pen = q2(raw_pen)
        if cap is not None and pen > cap:
            pen = cap

        batch_results[batch_id] = {
            "batch_id": batch_id,
            "merchant_id": merchant_id,
            "tier": tier,
            "currency": currency,
            "settled_amount_cny": float(q2(total_cny)),
            "breached_tx_count": breached_cnt,
            "duplicate_risk_count": dup_cnt,
            "penalty_cny": float(pen),
        }

    total_pen = q2(sum(Decimal(str(b["penalty_cny"])) for b in batch_results.values()))
    return {
        "root_cause_service": "risk-engine",
        "first_onset_node": "risk-node-02",
        "onset_time_utc8": "2026-11-24T14:13:25+08:00",
        "transient_recovered_node": "risk-node-04",
        "culprit_version": "v2.8.4",
        "bottleneck_span": "acquire_rule_lock",
        "isolated_nodes": ["risk-node-02", "risk-node-03"],
        "active_nodes": [
            "gw-edge-01", "gw-edge-02", "settle-node-01", "settle-node-02",
            "risk-node-01", "risk-node-04", "risk-node-05", "pg-primary-01",
        ],
        "rebalance_constraints": {
            "active_risk_nodes": {"risk-node-01": 30, "risk-node-04": 45, "risk-node-05": 55},
            "isolated_zero_nodes": ["risk-node-02", "risk-node-03"],
            "total_weight": 100,
        },
        "freeze_window_initial_utc8": ["17:30", "19:30"],
        "freeze_window_extended_utc8": ["17:30", "20:00"],
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
    generate_telemetry_series(sd / "telemetry_series.json")
    generate_openmetrics(sd / "metrics.openmetrics")
    truth = generate_logs_and_batches(ws / "data")
    pathlib.Path(args.truth_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.truth_path).write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    print("v0.5.0 generated! Total penalty CNY:", truth["total_penalty_cny"])
    print("Batches:", json.dumps(truth["batches"], ensure_ascii=False))
    print("Dup risk tx_ids:", truth["duplicate_risk_tx_ids"])


if __name__ == "__main__":
    main()
