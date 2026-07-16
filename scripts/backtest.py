"""Backtest harness (plan items 46, 55): would the detector have caught known
trends early?

For each ground-truth trend, pulls its historical daily news volume from GDELT
(date-ranged, free) and replays the production detector logic (same-weekday
Poisson baseline, same thresholds) over the series. Reports the first
detection date vs the trend's takeoff and mainstream months.

  detected EARLY  = flagged before the mainstream month began (the goal)
  detected LATE   = flagged only after mainstream
  missed          = never flagged

Usage:
    python -m scripts.backtest [--limit N] [--p-threshold 0.001]
"""

import argparse
import sys
import time
from datetime import date, datetime, timedelta

import requests
import yaml
from dotenv import load_dotenv

from analysis.anomalies import poisson_sf

DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
WARMUP_DAYS = 60


def month_start(ym: str) -> date:
    y, m = ym.split("-")
    return date(int(y), int(m), 1)


def fetch_series(query: str, start: date, end: date) -> dict[date, float]:
    for attempt in range(4):
        resp = requests.get(DOC_URL, params={
            "query": f'"{query}"', "mode": "timelinevolraw", "format": "json",
            "startdatetime": start.strftime("%Y%m%d") + "000000",
            "enddatetime": end.strftime("%Y%m%d") + "235959",
        }, timeout=60)
        if resp.status_code == 429 and attempt < 3:   # GDELT rate limit — back off
            time.sleep(15 * (attempt + 1))
            continue
        break
    resp.raise_for_status()
    series = {}
    for point in (resp.json().get("timeline") or [{}])[0].get("data", []):
        digits = "".join(ch for ch in point.get("date", "") if ch.isdigit())[:8]
        if len(digits) == 8:
            d = date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            series[d] = float(point.get("value") or 0)
    return series


def first_detection(series: dict[date, float], p_threshold: float,
                    min_mentions: float = 3) -> date | None:
    """Replay of the production surge detector over a historical series."""
    if not series:
        return None
    days = sorted(series)
    for d in days:
        if (d - days[0]).days < WARMUP_DAYS:
            continue
        today = series.get(d, 0)
        if today < min_mentions:
            continue
        samples = [series.get(d - timedelta(days=7 * k), 0.0) for k in range(1, 9)
                   if d - timedelta(days=7 * k) >= days[0]]
        if len(samples) < 3:
            continue
        lam = max(0.1, sum(samples) / len(samples))
        if today >= 1.5 * lam and poisson_sf(today, lam) < p_threshold:
            return d
    return None


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="0 = all trends")
    parser.add_argument("--p-threshold", type=float, default=0.001)
    args = parser.parse_args()

    with open("data/ground_truth.yaml") as f:
        trends = yaml.safe_load(f)["trends"]
    if args.limit:
        trends = trends[:args.limit]

    early = late = missed = no_data = 0
    leads = []
    print(f"{'trend':<28} {'takeoff':<9} {'mainstream':<11} {'detected':<12} verdict")
    print("-" * 78)
    for t in trends:
        takeoff, mainstream = month_start(str(t["takeoff"])), month_start(str(t["mainstream"]))
        query = (t.get("aliases") or [t["name"].replace("-", " ")])[0]
        start = takeoff - timedelta(days=170)
        end = min(mainstream + timedelta(days=90), date.today())
        try:
            series = fetch_series(query, start, end)
        except requests.RequestException as exc:
            print(f"{t['name']:<28} fetch failed: {str(exc)[:60]}")
            no_data += 1
            time.sleep(6)
            continue
        if len(series) < 90 or sum(series.values()) < 50:
            print(f"{t['name']:<28} {str(takeoff):<9} {str(mainstream):<11} {'—':<12} no news data")
            no_data += 1
            time.sleep(6)
            continue
        det = first_detection(series, args.p_threshold)
        if det is None:
            verdict, missed = "❌ missed", missed + 1
        elif det < mainstream:
            verdict, early = f"✅ EARLY ({(mainstream - det).days}d lead)", early + 1
            leads.append((mainstream - det).days)
        else:
            verdict, late = "⚠️ late", late + 1
        print(f"{t['name']:<28} {str(takeoff):<9} {str(mainstream):<11} {str(det or '—'):<12} {verdict}")
        time.sleep(6)

    graded = early + late + missed
    print("-" * 78)
    if graded:
        print(f"RECALL (early): {early}/{graded} = {100 * early // graded}%   "
              f"late: {late}   missed: {missed}   no-data: {no_data}")
    if leads:
        leads.sort()
        print(f"lead time (days before mainstream): median {leads[len(leads) // 2]}, "
              f"min {leads[0]}, max {leads[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
