"""Orchestrator (plan item 6): runs every enabled collector, records the run,
stores raw items with dedupe, tracks costs, and sends the daily summary.

Usage:
    python -m pipeline.orchestrator            # full daily run
    python -m pipeline.orchestrator --check    # validate config + list collectors, no DB
    python -m pipeline.orchestrator --init-db  # apply schema and exit
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from collectors.amazon import AmazonCollector
from collectors.base import BaseCollector
from collectors.google_trends import GoogleTrendsCollector
from collectors.reddit import RedditCollector
from collectors.tiktok import TikTokCollector
from pipeline import db
from reports.run_summary import send_summary

log = logging.getLogger("orchestrator")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

COLLECTORS: list[type[BaseCollector]] = [
    RedditCollector,
    GoogleTrendsCollector,
    AmazonCollector,
    TikTokCollector,
]


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def check(config: dict) -> int:
    print("config OK. Collectors:")
    for cls in COLLECTORS:
        c = cls(config)
        if not c.enabled:
            state = "disabled"
        else:
            reason = c.ready()
            state = f"BLOCKED: {reason}" if reason else "ready"
        print(f"  - {c.name:15s} {state}")
    return 0


def run(config: dict) -> int:
    conn = db.connect()
    db.apply_schema(conn)  # idempotent
    run_id = db.start_run(conn)
    log.info("run %d started", run_id)

    results = []
    any_failed = False
    for cls in COLLECTORS:
        collector = cls(config)
        if not collector.enabled:
            db.record_collector_run(conn, run_id, collector.name, "skipped", error="disabled in config")
            results.append((collector.name, "skipped", 0, 0, "disabled"))
            continue
        reason = collector.ready()
        if reason:
            db.record_collector_run(conn, run_id, collector.name, "skipped", error=reason)
            results.append((collector.name, "skipped", 0, 0, reason))
            log.warning("%s skipped: %s", collector.name, reason)
            continue

        t0 = time.monotonic()
        try:
            items = collector.fetch()
            stored = db.store_raw_items(conn, collector.name, items)
            duration = time.monotonic() - t0
            db.record_collector_run(conn, run_id, collector.name, "ok",
                                    items_seen=len(items), items_stored=stored,
                                    duration_s=round(duration, 1))
            results.append((collector.name, "ok", len(items), stored, None))
            log.info("%s: %d seen, %d new (%.1fs)", collector.name, len(items), stored, duration)
        except Exception as exc:  # a failing source must not kill the run
            any_failed = True
            duration = time.monotonic() - t0
            db.record_collector_run(conn, run_id, collector.name, "failed",
                                    duration_s=round(duration, 1), error=str(exc)[:2000])
            results.append((collector.name, "failed", 0, 0, str(exc)))
            log.exception("%s failed", collector.name)
        finally:
            for operation, units, cost in collector.costs:
                db.record_cost(conn, run_id, collector.name, operation, units, cost)

    # pipeline steps after collection: entity extraction (Haiku) + signals
    from pipeline.entities import run_extraction
    from pipeline.signals import build_daily_signals
    try:
        extraction = run_extraction(conn, run_id)
        if extraction["status"] == "ok":
            signal_rows = build_daily_signals(conn)
            results.append(("entity_extraction", "ok",
                            extraction["items"], extraction["mentions"], None))
            results.append(("signals_builder", "ok", signal_rows, signal_rows, None))
        else:
            results.append(("entity_extraction", "skipped", 0, 0, extraction.get("reason")))
    except Exception as exc:
        any_failed = True
        results.append(("entity_extraction", "failed", 0, 0, str(exc)))
        log.exception("extraction/signals failed")

    spend = db.month_spend(conn)
    status = "partial" if any_failed else "ok"
    if all(r[1] != "ok" for r in results):
        # nothing collected: real failures → failed; everything merely skipped → empty
        status = "failed" if any_failed else "empty"
    db.finish_run(conn, run_id, status, {
        "collectors": [{"name": n, "status": s, "seen": seen, "stored": st, "error": e}
                       for n, s, seen, st, e in results],
        "month_spend": dict(spend),
    })
    log.info("run %d finished: %s", run_id, status)

    send_summary(config, run_id, status, results, spend)
    return 0 if status != "failed" else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate config, no DB")
    parser.add_argument("--init-db", action="store_true", help="apply schema and exit")
    args = parser.parse_args()

    config = load_config()
    if args.check:
        return check(config)
    if args.init_db:
        conn = db.connect()
        db.apply_schema(conn)
        print("schema applied")
        return 0
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
