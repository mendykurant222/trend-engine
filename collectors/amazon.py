"""Amazon collector via Keepa (plan item 14). No self-scraping.

Needs KEEPA_KEY. Phase 1: fill config sources.amazon.categories with Keepa
category node ids (https://keepa.com/#!categorytree).
"""

import os
from datetime import date

from .base import BaseCollector

API = "https://api.keepa.com"


class AmazonCollector(BaseCollector):
    name = "amazon"
    min_interval_s = 1.0

    def ready(self) -> str | None:
        if not os.environ.get("KEEPA_KEY"):
            return "KEEPA_KEY not set"
        if not self.config.get("categories"):
            return "no Keepa category ids configured (sources.amazon.categories)"
        return None

    def fetch(self) -> list[dict]:
        key = os.environ["KEEPA_KEY"]
        domain = int(self.config.get("domain", 1))
        items: list[dict] = []
        today = date.today().isoformat()
        for cat in self.config.get("categories", []):
            resp = self.request(
                "GET", f"{API}/bestsellers",
                params={"key": key, "domain": domain, "category": cat},
            )
            data = resp.json()
            tokens = data.get("tokensConsumed", 1)
            self.add_cost("bestsellers", tokens,
                          tokens * float(self.config.get("cost_per_token_usd", 0.0005)))
            asins = data.get("bestSellersList", {}).get("asinList", [])
            if asins:
                items.append({
                    "external_id": f"bestsellers:{cat}:{today}",
                    "item_date": today,
                    "payload": {
                        "type": "bestsellers",
                        "category": cat,
                        "domain": domain,
                        "asins": asins[:200],  # rank = position in list
                    },
                })
        # TODO Phase 1: Movers & Shakers (Rainforest API or Keepa product queries)
        return items
