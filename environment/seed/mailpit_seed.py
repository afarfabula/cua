#!/usr/bin/env python3
"""v0.5.0 Mailpit seeder: sends 6 realistic threads including VOID draft scan distractor,
red-pen annotated REV-2 errata scan PDF, two-image visual SLA corridor matrix + legend appendix,
and non-spoiler DBA pool chart PNG.
"""
import argparse
import email.encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pathlib
import smtplib
import time


def send_mail(smtp_host, smtp_port, sender, recipients, subject, body, attachments=None):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for path in attachments or []:
        p = pathlib.Path(path)
        ctype = "application/pdf" if p.suffix.lower() == ".pdf" else "image/png"
        maintype, subtype = ctype.split("/", 1)
        part = MIMEBase(maintype, subtype)
        part.set_payload(p.read_bytes())
        email.encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=p.name)
        msg.attach(part)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
        s.sendmail(sender, recipients, msg.as_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-dir", required=True)
    ap.add_argument("--smtp-host", default="127.0.0.1")
    ap.add_argument("--smtp-port", type=int, default=1025)
    args = ap.parse_args()

    sd = pathlib.Path(args.seed_dir)
    for _ in range(20):
        try:
            with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=2):
                break
        except Exception:
            time.sleep(0.5)

    # 1. Routine history lock reminder
    send_mail(
        args.smtp_host, args.smtp_port,
        "infra-digest@clearing.local", ["sre-oncall@clearing.local"],
        "[Daily Digest] 2026-11-23 EOD Clearing Summary",
        "All 2026-11-23 settlement batches (BATCH-20261123-01, BATCH-20261123-02) have been audited and LOCKED.\nDo not modify locked historical records.",
    )

    # 2. Distractor earlier draft (VOID / SUPERSEDED raster PDF, no seal)
    send_mail(
        args.smtp_host, args.smtp_port,
        "pmo-drafts@clearing.local", ["sre-oncall@clearing.local"],
        "[FYI - Earlier Draft] Discussion draft v1.5 for VIP SLA adjustment",
        "Hi team,\n\nSharing the discussion draft v1.5 circulated earlier this week. Please verify against Compliance's final signed notice before applying any numbers.",
        attachments=[sd / "sla_draft_v1_5_void.pdf"],
    )

    # 3. DBA alert with pure chart PNG (no conclusion spoilers)
    send_mail(
        args.smtp_host, args.smtp_port,
        "dba-oncall@clearing.local", ["sre-oncall@clearing.local", "dev-team@clearing.local"],
        "[URGENT DBA] pg-primary-01 Connection Saturation & Lock Snapshot",
        "Hi SRE,\n\nAttached is the pg-primary-01 connection pool and lock-wait snapshot from yesterday's incident.\n"
        "Please review the cluster hard capacity limits in the attached chart before deciding on any open pool/timeout PRs in Gitea.",
        attachments=[sd / "dba_pg_lock_graph.png"],
    )

    # 4. Compliance & CAB official red-pen annotated errata PDF + two-image visual SLA matrix & legend
    send_mail(
        args.smtp_host, args.smtp_port,
        "compliance@clearing.local", ["sre-oncall@clearing.local", "finance-audit@clearing.local"],
        "【正式签发】2026-11-24 监管临时 SLA 勘误（REV-2）、视觉通道矩阵与图例附录",
        "各位好：\n\n"
        "合规部与变更委员会已正式签发带红笔修订批注与红色审批章的勘误扫描件（compliance_errata_20261124.pdf），以及配套的视觉通道矩阵图（sla_corridor_matrix.png）与图例对照附录（sla_legend_appendix.png）。\n"
        "通道的具体赔付档位、批次封顶、维护窗免责及互斥赔付标记均以视觉矩阵图例对照为准；该正式文件自 2026-11-24 00:00:00+08:00 起生效并作废此前旧制度及未签章草案。另请结合 /workspace/contracts/vip_merchant_framework_2026.pdf 核对个别商户的合同条款。",
        attachments=[sd / "compliance_errata_20261124.pdf", sd / "sla_corridor_matrix.png", sd / "sla_legend_appendix.png"],
    )

    # 5. Merchant ops reminder
    send_mail(
        args.smtp_host, args.smtp_port,
        "merchant-ops@clearing.local", ["sre-oncall@clearing.local"],
        "Reminder: Sandbox Test Merchants & Timezone Cut-off",
        "Hi,\n\nRemember TEST_ prefixed sandbox traffic in gateway_requests.jsonl must be excluded, and UTC ('Z') timestamps must be converted to UTC+8 before filtering 2026-11-24 batches.",
    )

    # 6. SRE Lead task assignment
    send_mail(
        args.smtp_host, args.smtp_port,
        "sre-lead@clearing.local", ["sre-oncall@clearing.local"],
        "[Action Required] 2026-11-24 Cascade Incident Root Cause, Rebalance, Canary & Audit Sign-off",
        "Hi Oncall,\n\nPlease use the CMDB high-resolution telemetry drill-down and Perfetto Trace to identify the earliest failing node, transient recovered node, and bottleneck span. "
        "Review open Gitea PRs carefully, push a full fix to risk-engine main, isolate only confirmed persistently faulty nodes, and rebalance active risk-engine traffic weights to 100% within topology capacity and canary limits. "
        "Run Portal Canary Replay, respond to CAB dynamic revision feedback until APPROVED, audit the 4 batches, and reply to sre-lead@clearing.local with root cause service, verified main commit SHA, canary_run_fingerprint, and total audited SLA penalty (CNY).\n",
    )
    print("Seeded 6 v0.5.0 Mailpit emails successfully.")


if __name__ == "__main__":
    main()