"""Phase 4 test — daily report, alerts, weekly report + performance table.

No LLM calls. Seeds trends/anomalies directly (including a backdated
trend_reports row so the performance table has something to grade), builds
both reports, SENDS real samples to Telegram (marked as demo), verifies
content, and cleans up.

Usage: python -m scripts.report_test
"""

import sys
from datetime import date, timedelta

from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from pipeline import db
from pipeline.orchestrator import load_config
from reports.channels import send_telegram
from reports.daily_report import build_daily_report, check_alerts, log_reported
from reports.weekly_report import build_weekly_report

TODAY = date.today()


def seed(conn):
    ids = {"entities": [], "clusters": []}

    def entity(name, cat):
        eid = conn.execute(
            "insert into entities (canonical_name, category, first_seen) values (%s, %s, %s) returning id",
            (name, cat, TODAY - timedelta(days=20))).fetchone()[0]
        ids["entities"].append(eid)
        return eid

    def cluster(name, cat, stage, strength, conf, days_old, entity_ids, tickers):
        cid = conn.execute(
            """insert into trend_clusters (name, category, stage, strength, confidence,
               first_detected, last_updated) values (%s,%s,%s,%s,%s,%s,%s) returning id""",
            (name, cat, stage, strength, conf, TODAY - timedelta(days=days_old), TODAY)
        ).fetchone()[0]
        ids["clusters"].append(cid)
        for eid in entity_ids:
            conn.execute("insert into trend_cluster_entities values (%s, %s)", (cid, eid))
        for ticker, exposure, direction, material in tickers:
            comp = conn.execute("select id from companies where ticker = %s", (ticker,)).fetchone()
            if comp:
                conn.execute(
                    """insert into trend_companies (cluster_id, company_id, exposure, direction, confidence, material)
                       values (%s,%s,%s,%s,80,%s)""",
                    (cid, comp[0], exposure, direction, material))
        return cid

    e1 = entity("demo pool lamp", "home")
    e2 = entity("demo solar light", "home")
    e3 = entity("demo energy drink", "food-beverage")
    c1 = cluster("demo: solar pool lighting", "home", "accelerating", 82, 70, 16,
                 [e1, e2], [("POOL", "retailer", "positive", True),
                            ("HAYW", "manufacturer", "positive", False)])
    cluster("demo: functional energy drinks", "food-beverage", "emerging", 55, 40, 3,
            [e3], [("CELH", "manufacturer", "positive", True),
                   ("MNST", "manufacturer", "negative", False)])

    # extreme cross-source anomaly for the alert path
    conn.execute(
        """insert into anomalies (entity_id, source, signal_date, kind, score, details)
           values (%s, '_synthetic', %s, 'surge', 96, %s)""",
        (e1, TODAY, Jsonb({"today": 22, "baseline_mean": 1.4, "p": 1e-12, "cross_source": True})))

    # backdated report row so the 14d performance table has a graded call
    conn.execute(
        """insert into trend_reports (cluster_id, reported_date, stage, strength, confidence)
           values (%s, %s, 'emerging', 60, 50)""",
        (c1, TODAY - timedelta(days=14)))
    return ids


def main() -> int:
    load_dotenv()
    conn = db.connect()
    db.apply_schema(conn)
    config = load_config()
    failures = []
    ids = {"entities": [], "clusters": []}

    try:
        ids = seed(conn)

        daily, reported = build_daily_report(conn, config)
        print(daily, "\n")
        for needle, label in [("demo: solar pool lighting", "top trend"),
                              ("POOL", "ticker"), ("★", "material mark"),
                              ("active trends; strongest", "summary line"),
                              ("/trend/", "dashboard deep link"),
                              ("day 16", "trend age")]:
            if needle not in daily:
                failures.append(f"daily report missing {label} ({needle!r})")
        if len(reported) != 2:
            failures.append(f"expected 2 reported trends, got {len(reported)}")
        log_reported(conn, reported)

        alerts = check_alerts(conn, config)
        print("\n".join(alerts) or "(no alerts)", "\n")
        if len(alerts) != 1 or "demo pool lamp" not in alerts[0]:
            failures.append(f"expected 1 alert for demo pool lamp, got {len(alerts)}")

        weekly = build_weekly_report(conn, config)
        print(weekly)
        for needle, label in [("60→82", "performance delta"),
                              ("reported 14d ago", "14d grading"),
                              ("✅", "positive verdict"),
                              ("Week in review", "summary line")]:
            if needle not in weekly:
                failures.append(f"weekly report missing {label} ({needle!r})")

        # deliver real samples so Mendy sees the actual format
        send_telegram("🧪 <b>DEMO — sample reports below (synthetic data, will not repeat)</b>")
        send_telegram(daily)
        for a in alerts:
            send_telegram(a)
        send_telegram(weekly)
        print("\nsample reports sent to Telegram")

    finally:
        cids, eids = ids["clusters"], ids["entities"]
        if cids:
            conn.execute("delete from trend_reports where cluster_id = any(%s)", (cids,))
            conn.execute("delete from trend_companies where cluster_id = any(%s)", (cids,))
            conn.execute("delete from trend_evidence where cluster_id = any(%s)", (cids,))
            conn.execute("delete from trend_cluster_entities where cluster_id = any(%s)", (cids,))
            conn.execute("delete from trend_clusters where id = any(%s)", (cids,))
        if eids:
            conn.execute("delete from anomalies where entity_id = any(%s)", (eids,))
            conn.execute("delete from entities where id = any(%s)", (eids,))
        print("cleanup done")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nREPORT TEST PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
