#!/usr/bin/env python3
"""v2 Mailpit seeder: sends 6 realistic threads including VOID draft distractor,
red-sealed REV-2 errata PNG+PDF, color SLA corridor matrix PNG, and DBA lock snapshot PNG.
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

    # 2. Distractor earlier draft (VOID / SUPERSEDED PDF, no seal)
    send_mail(
        args.smtp_host, args.smtp_port,
        "pmo-drafts@clearing.local", ["sre-oncall@clearing.local"],
        "[FYI - Earlier Draft] Discussion draft v1.5 for VIP SLA adjustment",
        "Hi team,\n\nSharing the discussion draft v1.5 circulated earlier this week. Please verify against Compliance's final signed notice before applying any numbers.",
        attachments=[sd / "sla_draft_v1_5_void.pdf"],
    )

    # 3. DBA alert with PNG diagram (warns on Gitea PR #1)
    send_mail(
        args.smtp_host, args.smtp_port,
        "dba-oncall@clearing.local", ["sre-oncall@clearing.local", "dev-team@clearing.local"],
        "[URGENT DBA] pg-primary-01 Connection Saturation & Warning on Gitea PR #1",
        "Hi SRE,\n\nAttached is the lock-wait and connection pool snapshot from pg-primary-01 during yesterday's incident.\n"
        "Please check the attached PNG topology diagram carefully before handling open PR #1 in Gitea!",
        attachments=[sd / "dba_pg_lock_graph.png"],
    )

    # 4. Compliance & CAB official red-sealed REV-2 errata + color-coded corridor matrix!
    send_mail(
        args.smtp_host, args.smtp_port,
        "compliance@clearing.local", ["sre-oncall@clearing.local", "finance-audit@clearing.local"],
        "【正式签发】2026-11-24 监管临时 SLA 勘误（REV-2）、颜色矩阵与变更冻结通知",
        "各位好：\n\n"
        "合规部与变更委员会已正式签发红章版勘误通知（见附件 sla_errata_stamp.png 与 compliance_errata_20261124.pdf），以及按商户/通道着色的可视化 SLA 矩阵图（sla_corridor_matrix.png）。\n"
        "该正式文件自 2026-11-24 00:00:00+08:00 起生效并作废此前所有旧制度及未签章草案；另请注意结合 /workspace/contracts/vip_merchant_framework_2026.pdf 核对个别商户的维护窗免责条款。",
        attachments=[sd / "sla_errata_stamp.png", sd / "compliance_errata_20261124.pdf", sd / "sla_corridor_matrix.png"],
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
        "[Action Required] 2026-11-24 Cascade Incident Root Cause, Canary Replay & Audit Sign-off",
        "Hi Oncall,\n\nPlease verify the cascade timeline in Grafana and Perfetto first, fix the root cause in Gitea, run the Portal Canary Replay until Grafana & Perfetto confirm green recovery, then complete CMDB isolation, Release Approval, and SLA Batch Audit.\n"
        "Finally reply to sre-lead@clearing.local with root cause service, onset time (UTC+8), verified main commit SHA, and total audited SLA penalty (CNY).\n",
    )
    print("Seeded 6 v2 Mailpit emails successfully.")


if __name__ == "__main__":
    main()
