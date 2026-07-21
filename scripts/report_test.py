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


def purge_demo(conn):
    """Remove any demo rows left behind by an interrupted run."""
    eids = [r[0] for r in conn.execute(
        "select id from entities where canonical_name like 'demo %'").fetchall()]
    cids = [r[0] for r in conn.execute(
        "select id from trend_clusters where name like 'demo:%'").fetchall()]
    if cids:
        for tbl in ("trend_reports", "trend_companies", "trend_evidence",
                    "trend_cluster_entities"):
            conn.execute(f"delete from {tbl} where cluster_id = any(%s)", (cids,))
        conn.execute("delete from trend_clusters where id = any(%s)", (cids,))
    if eids:
        for tbl in ("anomalies", "daily_signals", "raw_item_entities", "entity_aliases"):
            conn.execute(f"delete from {tbl} where entity_id = any(%s)", (eids,))
        conn.execute("delete from entities where id = any(%s)", (eids,))


def seed(conn):
    ids = {"entities": [], "clusters": []}

    purge_demo(conn)          # a crashed previous run must not block this one

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

    def climbing_signals(eid, base=30):
        """Week-over-week growth so the demo trends earn real momentum —
        the report ranks by momentum now, not by strength."""
        for days_ago in range(14):
            value = base * (3 if days_ago < 7 else 1)     # this week 3x last week
            for source in ("_synthetic", "_synthetic2"):
                conn.execute(
                    """insert into daily_signals (entity_id, source, signal_date, metric, value)
                       values (%s, %s, %s, 'mentions', %s)
                       on conflict (entity_id, source, signal_date, metric)
                       do update set value = excluded.value""",
                    (eid, source, TODAY - timedelta(days=days_ago), value))

    e1 = entity("demo pool lamp", "home")
    e2 = entity("demo solar light", "home")
    e3 = entity("demo energy drink", "food-beverage")
    for eid in (e1, e2, e3):
        climbing_signals(eid)
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

        # widen the report for the fixture assertions: real production trends
        # legitimately outrank demo data, and the test is about format + logic
        wide = {**config, "reports": {**config.get("reports", {}), "daily_top_n": 50}}
        daily, reported = build_daily_report(conn, wide)
        print(daily, "\n")
        for needle, label in [("demo: solar pool lighting", "top trend"),
                              ("POOL", "ticker"), ("★", "material mark"),
                              ("Fastest-rising", "momentum summary line"),
                              ("momentum ", "momentum score"),
                              ("wow", "week-over-week growth"),
                              ("/trend/", "dashboard deep link"),
                              ("day 16", "trend age")]:
            if needle not in daily:
                failures.append(f"daily report missing {label} ({needle!r})")
        # real trends coexist with the demo ones — assert containment, not count
        if not set(ids["clusters"]).issubset(set(reported)):
            failures.append(f"demo trends missing from report: {ids['clusters']} vs {reported}")
        log_reported(conn, reported)

        alerts = check_alerts(conn, config)
        print("\n".join(alerts) or "(no alerts)", "\n")
        # every alert must be a well-formed cross-source/watchlist alert; the
        # demo anomaly (96) may be outranked by real ones, so assert the rule
        for a in alerts:
            if "Trend Alert" not in a or ("cross-source" not in a and "watchlist" not in a):
                failures.append(f"malformed alert: {a[:60]}")
        if not alerts:
            failures.append("no alerts fired despite a score-96 cross-source anomaly")

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
            conn.execute("delete from daily_signals where entity_id = any(%s)", (eids,))
            conn.execute("delete from entity_aliases where entity_id = any(%s)", (eids,))
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
