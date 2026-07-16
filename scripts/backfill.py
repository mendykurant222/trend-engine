"""Historical backfill (plan items 10, 53).

google_trends: one SerpApi TIMESERIES call per active entity returns ~90 days
of self-consistent daily interest — instant baselines, like GDELT's timelines.
(gdelt backfills itself on every normal run; reddit/amazon backfill need those
sources' history access and land with them.)

Usage:
    python -m scripts.backfill --source google_trends [--limit 25]
"""

import argparse
import logging
import sys

import yaml
from dotenv import load_dotenv

from collectors.google_trends import GoogleTrendsCollector
from pipeline import db
from pipeline.signals import build_daily_signals


def backfill_google_trends(conn, config: dict, limit: int) -> int:
    entities = conn.execute(
        """select id, canonical_name from entities where status = 'active'
           order by first_seen desc limit %s""", (limit,)).fetchall()
    if not entities:
        print("no active entities to backfill")
        return 0
    collector = GoogleTrendsCollector(config)
    reason = collector.ready()
    if reason:
        raise SystemExit(f"cannot backfill: {reason}")
    items = collector.fetch_interest([(e[0], e[1]) for e in entities])
    stored = db.store_raw_items(conn, "google_trends", items)
    for operation, units, cost in collector.costs:
        db.record_cost(conn, None, "google_trends", f"backfill_{operation}", units, cost)
    rows = build_daily_signals(conn)
    print(f"backfilled {len(items)} entities ({stored} new items), {rows} signal rows upserted")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["google_trends"], required=True)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    conn = db.connect()
    db.apply_schema(conn)
    return backfill_google_trends(conn, config, args.limit)


if __name__ == "__main__":
    sys.exit(main())
