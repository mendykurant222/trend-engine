"""Seed/clean demo data for dashboard verification.

Usage: python -m scripts.dashboard_demo seed|clean
Everything is prefixed 'demo:' / uses source '_synthetic' and is fully
removed by `clean`.
"""

import sys
from datetime import date, timedelta

from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from pipeline import db

TODAY = date.today()


def seed(conn):
    def entity(name, cat, days_old):
        return conn.execute(
            """insert into entities (canonical_name, category, first_seen)
               values (%s, %s, %s) on conflict (canonical_name) do update set category=excluded.category
               returning id""",
            (name, cat, TODAY - timedelta(days=days_old))).fetchone()[0]

    def signals(eid, source, series):  # series: {days_ago: value}
        for days_ago, v in series.items():
            conn.execute(
                """insert into daily_signals (entity_id, source, signal_date, metric, value)
                   values (%s, %s, %s, 'mentions', %s)
                   on conflict (entity_id, source, signal_date, metric) do update set value=excluded.value""",
                (eid, source, TODAY - timedelta(days=days_ago), v))

    e1 = entity("demo: floating pool lamp", "home", 24)
    e2 = entity("demo: solar pool light", "home", 12)
    e3 = entity("demo: mushroom gummies", "wellness", 30)

    # ramping series for e1/e2, flat-then-dead for e3
    signals(e1, "_synthetic", {i: max(0, 8 - i // 3) for i in range(0, 24)})
    signals(e1, "gdelt", {i: max(0, 14 - i) for i in range(0, 14)})
    signals(e2, "_synthetic", {i: max(0, 5 - i // 2) for i in range(0, 12)})
    signals(e3, "_synthetic", {i: 3 if i > 15 else 0 for i in range(0, 30)})

    def cluster(name, cat, stage, strength, conf, days_old, status, eids, lifecycle_days):
        lifecycle = [{"date": str(TODAY - timedelta(days=d)),
                      "stage": stage, "strength": strength - d * 2, "confidence": conf - d}
                     for d in range(lifecycle_days, -1, -1)]
        cid = conn.execute(
            """insert into trend_clusters (name, category, stage, strength, confidence,
               status, first_detected, last_updated, lifecycle)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
            (name, cat, stage, strength, conf, status,
             TODAY - timedelta(days=days_old), TODAY, Jsonb(lifecycle))).fetchone()[0]
        for eid in eids:
            conn.execute("insert into trend_cluster_entities values (%s,%s)", (cid, eid))
        return cid

    c1 = cluster("demo: solar pool lighting", "home", "accelerating", 82, 71, 14, "active", [e1, e2], 10)
    cluster("demo: mushroom gummies", "wellness", "declining", 25, 60, 30, "dead", [e3], 8)

    for ticker, exp, direction, material in [("POOL", "retailer", "positive", True),
                                             ("HAYW", "manufacturer", "positive", False),
                                             ("LESL", "retailer", "positive", False)]:
        comp = conn.execute("select id from companies where ticker=%s", (ticker,)).fetchone()
        if comp:
            conn.execute(
                """insert into trend_companies (cluster_id, company_id, exposure, direction, confidence, material)
                   values (%s,%s,%s,%s,75,%s)""", (c1, comp[0], exp, direction, material))

    for days_ago, src, kind, score, details in [
        (0, "_synthetic", "surge", 96, {"today": 9, "baseline_mean": 1.8, "p": 1e-10, "cross_source": True}),
        (0, "gdelt", "surge", 88, {"today": 14, "baseline_mean": 2.1, "p": 1e-9, "cross_source": True}),
        (3, "_synthetic", "acceleration", 55, {"velocity_now": 5.3, "velocity_prev": 2.1, "ratio": 2.5}),
    ]:
        aid = conn.execute(
            """insert into anomalies (entity_id, source, signal_date, kind, score, details)
               values (%s,%s,%s,%s,%s,%s) returning id""",
            (e1, src, TODAY - timedelta(days=days_ago), kind, score, Jsonb(details))).fetchone()[0]
        conn.execute("insert into trend_evidence (cluster_id, anomaly_id) values (%s,%s)", (c1, aid))

    items = [{"external_id": None, "item_date": (TODAY - timedelta(days=i)).isoformat(),
              "payload": {"title": f"demo raw post about pool lamps #{i}", "score": 100 + i}}
             for i in range(4)]
    db.store_raw_items(conn, "_synthetic", items)
    for (rid,) in conn.execute("select id from raw_items where source='_synthetic'").fetchall():
        conn.execute("insert into raw_item_entities values (%s,%s) on conflict do nothing", (rid, e1))
    print("seeded")


def clean(conn):
    eids = [r[0] for r in conn.execute(
        "select id from entities where canonical_name like 'demo:%'").fetchall()]
    cids = [r[0] for r in conn.execute(
        "select id from trend_clusters where name like 'demo:%'").fetchall()]
    if cids:
        conn.execute("delete from trend_reports where cluster_id = any(%s)", (cids,))
        conn.execute("delete from trend_companies where cluster_id = any(%s)", (cids,))
        conn.execute("delete from trend_evidence where cluster_id = any(%s)", (cids,))
        conn.execute("delete from trend_cluster_entities where cluster_id = any(%s)", (cids,))
        conn.execute("delete from trend_clusters where id = any(%s)", (cids,))
    conn.execute("delete from raw_item_entities where raw_item_id in (select id from raw_items where source='_synthetic')")
    conn.execute("delete from raw_items where source='_synthetic'")
    if eids:
        conn.execute("delete from anomalies where entity_id = any(%s)", (eids,))
        conn.execute("delete from daily_signals where entity_id = any(%s)", (eids,))
        conn.execute("delete from entity_aliases where entity_id = any(%s)", (eids,))
        conn.execute("delete from entities where id = any(%s)", (eids,))
    print("cleaned")


def main() -> int:
    load_dotenv()
    conn = db.connect()
    db.apply_schema(conn)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "seed"
    {"seed": seed, "clean": clean}[cmd](conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
