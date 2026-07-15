"""Historical backfill (plan item 10) — gives the anomaly detector real 30/90-day
baselines from day one instead of two weeks of live data.

Per source:
  google_trends  SerpApi TIMESERIES with date ranges (full history available)
  amazon         Keepa product/category history (Keepa sells historical data)
  reddit         page backward through listings via the official API

Usage:
    python -m scripts.backfill --source google_trends --days 90
    python -m scripts.backfill --source reddit --days 90

Status: CLI skeleton — implementations land in Phase 1 alongside each collector,
using the same raw_items storage so Signals Builder doesn't care whether data
came from live runs or backfill.
"""

import argparse
import sys

from dotenv import load_dotenv

SOURCES = ["google_trends", "amazon", "reddit"]


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    print(f"TODO Phase 1: backfill {args.source} for {args.days} days")
    return 1


if __name__ == "__main__":
    sys.exit(main())
