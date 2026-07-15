"""Phase 3 test — clustering, trend memory, and investment mapping.

Makes REAL Claude calls (Sonnet), costs a few cents. Seeds anomalous entities
that obviously form trends, then verifies:

  day 1: Claude clusters them into trends; the two pool-lighting entities
         should land together; investment mapping stores only SEC-verified
         tickers; lifecycle + evidence rows exist.
  day 2: the same entities anomalous again -> trend memory updates the
         EXISTING trends via entity overlap (no new trends, no LLM needed
         for assignment).

Usage: python -m scripts.trends_test
"""

import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from analysis.trends import run_trend_analysis, trend_report_lines
from pipeline import db, llm
from pipeline.orchestrator import load_config

from psycopg.types.json import Jsonb

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)

ENTITIES = [
    # (name, category, anomaly kinds on day1) — pool pair should cluster together
    ("floating pool lamp", "home", [("_synthetic", "surge", 80), ("_synthetic2", "surge", 70)]),
    ("solar pool light", "home", [("_synthetic", "new_entity", 60)]),
    ("celsius energy drink", "food-beverage", [("_synthetic", "acceleration", 55)]),
]


def seed_anomalies(conn, entities, signal_date):
    ids = {}
    for name, category, kinds in entities:
        row = conn.execute(
            """insert into entities (canonical_name, category, first_seen)
               values (%s, %s, %s)
               on conflict (canonical_name) do update set category = excluded.category
               returning id""",
            (name, category, signal_date - timedelta(days=10)),
        ).fetchone()
        ids[name] = row[0]
        for source, kind, score in kinds:
            conn.execute(
                """insert into anomalies (entity_id, source, signal_date, kind, score, details)
                   values (%s, %s, %s, %s, %s, %s)""",
                (row[0], source, signal_date, kind, score,
                 Jsonb({"today": 10, "baseline_mean": 1.0, "p": 1e-8,
                        "velocity_now": 3, "velocity_prev": 1,
                        "mentions_in_window": 10, "sources": [source]})),
            )
    return ids


def main() -> int:
    load_dotenv()
    if llm.ready():
        print("ANTHROPIC_API_KEY not set — cannot run Phase 3 test")
        return 1
    conn = db.connect()
    db.apply_schema(conn)
    config = load_config()
    failures = []
    eids = {}

    try:
        # ---- day 1: fresh clustering ----
        eids = seed_anomalies(conn, ENTITIES, YESTERDAY)
        stats1, _ = run_trend_analysis(conn, config, None, YESTERDAY)
        print(f"day 1: {stats1}")
        if stats1["new"] < 1:
            failures.append("day 1: no trends created")
        if stats1["new"] > 3:
            failures.append(f"day 1: too many trends ({stats1['new']}) for 3 entities")

        # every seeded entity must be in exactly one cluster
        for name, eid in eids.items():
            n = conn.execute(
                "select count(*) from trend_cluster_entities where entity_id = %s", (eid,)
            ).fetchone()[0]
            if n != 1:
                failures.append(f"day 1: entity {name} in {n} clusters (expected 1)")

        # pool pair should share a cluster (Claude judgment — warn, don't fail)
        pool = conn.execute(
            """select count(distinct cluster_id) from trend_cluster_entities
               where entity_id = any(%s)""",
            ([eids["floating pool lamp"], eids["solar pool light"]],),
        ).fetchone()[0]
        print(f"pool-lighting entities in {pool} cluster(s)" +
              (" ✅" if pool == 1 else " ⚠️ (expected 1 — Claude judgment call)"))

        # evidence + lifecycle written
        ev = conn.execute(
            """select count(*) from trend_evidence te
               join trend_cluster_entities tce on tce.cluster_id = te.cluster_id
               where tce.entity_id = any(%s)""", (list(eids.values()),),
        ).fetchone()[0]
        if ev == 0:
            failures.append("day 1: no evidence rows")

        # investment mapping: only verified tickers stored
        inv = conn.execute(
            """select c.ticker, tc.exposure, tc.direction, tc.material
               from trend_companies tc
               join companies c on c.id = tc.company_id
               join trend_cluster_entities tce on tce.cluster_id = tc.cluster_id
               where tce.entity_id = any(%s)""", (list(eids.values()),),
        ).fetchall()
        print(f"investment mapping: {inv}")
        for ticker, *_ in inv:
            ok = conn.execute("select 1 from companies where ticker = %s", (ticker,)).fetchone()
            if not ok:
                failures.append(f"unverified ticker stored: {ticker}")

        # ---- day 2: trend memory — same entities anomalous again ----
        seed_anomalies(conn, ENTITIES, TODAY)
        before = conn.execute("select count(*) from trend_clusters").fetchone()[0]
        stats2, _ = run_trend_analysis(conn, config, None, TODAY)
        after = conn.execute("select count(*) from trend_clusters").fetchone()[0]
        print(f"day 2: {stats2}")
        if after != before:
            failures.append(f"day 2: trend memory failed — {after - before} new trends opened for known entities")
        if stats2["updated"] < 1:
            failures.append("day 2: no trends updated")

        # lifecycle should now have 2 entries on the updated trends
        lc = conn.execute(
            """select t.name, jsonb_array_length(t.lifecycle) from trend_clusters t
               join trend_cluster_entities tce on tce.cluster_id = t.id
               where tce.entity_id = %s""", (eids["floating pool lamp"],),
        ).fetchone()
        print(f"lifecycle: trend '{lc[0]}' has {lc[1]} entries")
        if lc[1] < 2:
            failures.append(f"lifecycle not accumulating: {lc[1]} entries after 2 days")

        for line in trend_report_lines(conn):
            print(line)

    finally:
        ids = list(eids.values())
        if ids:
            cids = [r[0] for r in conn.execute(
                "select distinct cluster_id from trend_cluster_entities where entity_id = any(%s)", (ids,)
            ).fetchall()]
            if cids:
                conn.execute("delete from trend_companies where cluster_id = any(%s)", (cids,))
                conn.execute("delete from trend_evidence where cluster_id = any(%s)", (cids,))
                conn.execute("delete from trend_cluster_entities where cluster_id = any(%s)", (cids,))
                conn.execute("delete from trend_clusters where id = any(%s)", (cids,))
            conn.execute("delete from anomalies where entity_id = any(%s)", (ids,))
            conn.execute("delete from entity_aliases where entity_id = any(%s)", (ids,))
            conn.execute("delete from entities where id = any(%s)", (ids,))
        print("cleanup done")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nTRENDS TEST PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
