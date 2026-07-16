"""TikTok Creative Center collector (plan items 15, 60).

Pulls the trending-hashtags ranking PER INDUSTRY via the doliz Apify actor —
the category depth the original plan asked for, not just the global top-100.
Works without TikTok cookies (verified live). Industry IDs were mapped by
probing (see docs/ARCHITECTURE.md); labels are ours.

Also a calibration benchmark: TikTok's own per-industry top-10 vs our
detector (analysis/benchmark.py).
"""

import os
from datetime import date

from .base import BaseCollector, CollectorError

RUN_SYNC = ("https://api.apify.com/v2/acts/"
            "doliz~tiktok-creative-center-scraper/run-sync-get-dataset-items")

DEFAULT_INDUSTRIES = [
    {"id": "", "label": "all"},
    {"id": "14000000000", "label": "beauty"},
    {"id": "15000000000", "label": "tech"},
    {"id": "18000000000", "label": "household"},
    {"id": "21000000000", "label": "home"},
    {"id": "22000000000", "label": "ecommerce"},
    {"id": "25000000000", "label": "games"},
    {"id": "27000000000", "label": "food"},
]


class TikTokCollector(BaseCollector):
    name = "tiktok"
    min_interval_s = 2.0
    max_retries = 2

    def ready(self) -> str | None:
        if not os.environ.get("APIFY_TOKEN"):
            return "APIFY_TOKEN not set"
        return None

    def fetch(self) -> list[dict]:
        industries = self.config.get("industries") or DEFAULT_INDUSTRIES
        limit = int(self.config.get("hashtags_limit", 25))
        period = str(self.config.get("period_days", 7))
        country = self.config.get("country", "US")
        cost = float(self.config.get("cost_per_run_usd", 0.05))
        today = date.today().isoformat()

        items: list[dict] = []
        for ind in industries:
            try:
                resp = self.request(
                    "POST", RUN_SYNC,
                    params={"token": os.environ["APIFY_TOKEN"]},
                    json={"target": "trending_hashtags",
                          "hashtags_country": country,
                          "hashtags_period": period,
                          "hashtags_industry": ind["id"],
                          "hashtags_limit": limit,
                          "cookies": ""},
                    timeout=300,
                )
            except CollectorError as exc:
                self.log.warning("industry %s failed: %s", ind["label"], exc)
                continue
            self.add_cost(f"hashtags_{ind['label']}", 1, cost)
            data = resp.json()
            rows = (data[0].get("items") or []) if isinstance(data, list) and data else []
            for i, h in enumerate(rows):
                items.append({
                    "external_id": f"hashtag:{ind['label']}:{h.get('hashtagName')}:{today}",
                    "item_date": today,
                    "payload": {"type": "creative_center", "row": {
                        "type": "hashtag",
                        "name": h.get("hashtagName"),
                        "rank": h.get("rankIndex") or i + 1,
                        "videoViews": h.get("vv"),
                        "publishCnt": h.get("publishCnt"),
                        "industry": ind["label"],
                    }},
                })
            self.log.info("industry %s: %d hashtags", ind["label"], len(rows))
        return items
