"""End-to-end pipeline smoke test with synthetic data — no API keys needed.

Inserts fake raw items under source '_synthetic', simulates the extraction
result (bypassing Claude), runs entity resolution + linking + the Signals
Builder, verifies the daily_signals rows, then deletes everything it created.

Usage: python -m scripts.smoke_test
"""

import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from pipeline import db
from pipeline.entities import link, resolve_mention
from pipeline.signals import build_daily_signals

SOURCE = "_synthetic"

POSTS = [
    # (days_ago, title, score, mentions [(name, category)])
    (0, "This Stanley tumbler changed my hydration game", 450, [("stanley tumbler", "consumer-products")]),
    (0, "Where to buy dubai chocolate in the US?", 210, [("dubai chocolate", "food-beverage")]),
    (1, "My STANLEY TUMBLER collection (photo)", 890, [("Stanley Tumbler", "consumer-products")]),  # alias case-variant
    (1, "Anyone tried a cold plunge tub at home?", 120, [("cold plunge tub", "wellness")]),
    (2, "Best water bottle for hiking?", 55, []),  # generic — no entities
]


def main() -> int:
    load_dotenv()
    conn = db.connect()
    db.apply_schema(conn)
    failures = []

    try:
        # 1) store raw items (exercises hash dedupe path)
        items = []
        for days_ago, title, score, _ in POSTS:
            d = (date.today() - timedelta(days=days_ago)).isoformat()
            items.append({"external_id": None, "item_date": d,
                          "payload": {"title": title, "selftext": "", "score": score}})
        stored = db.store_raw_items(conn, SOURCE, items)
        print(f"stored {stored}/{len(items)} synthetic raw items")

        # storing the same items again must dedupe to 0
        again = db.store_raw_items(conn, SOURCE, items)
        if again != 0:
            failures.append(f"dedupe failed: {again} duplicates stored")

        # 2) simulate extraction output -> resolution + linking
        rows = conn.execute(
            "select id, payload->>'title' from raw_items where source = %s order by id", (SOURCE,)
        ).fetchall()
        by_title = {title: rid for rid, title in rows}
        entity_ids = set()
        for _, title, _, mentions in POSTS:
            for name, category in mentions:
                eid = resolve_mention(conn, name, category)
                entity_ids.add(eid)
                link(conn, by_title[title], eid)

        # the two case-variant stanley mentions must resolve to ONE entity
        stanley = conn.execute(
            "select count(distinct entity_id) from entity_aliases where alias = 'stanley tumbler'"
        ).fetchone()[0]
        if stanley != 1:
            failures.append(f"alias resolution failed: {stanley} stanley entities")
        if len(entity_ids) != 3:
            failures.append(f"expected 3 distinct entities, got {len(entity_ids)}")

        # 3) signals builder
        build_daily_signals(conn)
        sig = conn.execute("""
            select e.canonical_name, s.signal_date, s.metric, s.value
            from daily_signals s join entities e on e.id = s.entity_id
            where s.source = %s order by 1, 2, 3
        """, (SOURCE,)).fetchall()
        for row in sig:
            print("  signal:", row)

        # stanley: 1 mention today + 1 yesterday; yesterday's score_sum = 890
        vals = {(n, str(d), m): float(v) for n, d, m, v in sig}
        today, yday = str(date.today()), str(date.today() - timedelta(days=1))
        checks = [
            (("stanley tumbler", today, "mentions"), 1),
            (("stanley tumbler", yday, "mentions"), 1),
            (("stanley tumbler", yday, "score_sum"), 890),
            (("dubai chocolate", today, "score_sum"), 210),
            (("cold plunge tub", yday, "mentions"), 1),
        ]
        for key, expected in checks:
            if vals.get(key) != expected:
                failures.append(f"signal {key}: expected {expected}, got {vals.get(key)}")

    finally:
        # 4) cleanup — only rows this test created
        eids = list(entity_ids) if "entity_ids" in dir() else []
        conn.execute("delete from daily_signals where source = %s", (SOURCE,))
        conn.execute(
            "delete from raw_item_entities where raw_item_id in (select id from raw_items where source = %s)",
            (SOURCE,),
        )
        conn.execute("delete from raw_items where source = %s", (SOURCE,))
        if eids:
            conn.execute("delete from daily_signals where entity_id = any(%s)", (eids,))
            conn.execute("delete from entity_aliases where entity_id = any(%s)", (eids,))
            conn.execute("delete from entities where id = any(%s)", (eids,))
        print("cleanup done")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nSMOKE TEST PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
