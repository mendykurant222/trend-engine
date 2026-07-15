"""TikTok Creative Center via Apify actor (plan item 15).

The most important consumer-trend source and the most fragile — no official API.
Needs APIFY_TOKEN + an actor id in config (pick one in Phase 1; several
"tiktok creative center" actors exist on Apify).

Also serves as a calibration benchmark: TikTok CC's own ranked trend lists are
ground truth — if our system misses what TikTok already declares trending, we
have a bug (plan items 15, 22).
"""

import os

from .base import BaseCollector, CollectorError

APIFY_RUN_SYNC = "https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"


class TikTokCollector(BaseCollector):
    name = "tiktok"
    min_interval_s = 2.0
    max_retries = 2

    def ready(self) -> str | None:
        if not os.environ.get("APIFY_TOKEN"):
            return "APIFY_TOKEN not set"
        if not self.config.get("actor_id"):
            return "no Apify actor_id configured (sources.tiktok.actor_id)"
        return None

    def fetch(self) -> list[dict]:
        actor = self.config["actor_id"].replace("/", "~")
        resp = self.request(
            "POST", APIFY_RUN_SYNC.format(actor_id=actor),
            params={"token": os.environ["APIFY_TOKEN"]},
            json=self.config.get("actor_input", {}),
            timeout=300,
        )
        self.add_cost("actor_run", 1, float(self.config.get("cost_per_run_usd", 0.10)))
        rows = resp.json()
        if not isinstance(rows, list):
            raise CollectorError(f"unexpected Apify response: {str(rows)[:200]}")
        return [{
            "external_id": None,
            "item_date": None,
            "payload": {"type": "creative_center", "row": row},
        } for row in rows]
