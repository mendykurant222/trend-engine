"""Google Trends via SerpApi — the PRIMARY path, pytrends dropped (plan item 12).

Needs SERPAPI_KEY. Every timeseries request includes the fixed anchor query so
values are comparable across days (Google returns relative 0-100 per request).
"""

import os

from .base import BaseCollector

SEARCH_URL = "https://serpapi.com/search.json"


class GoogleTrendsCollector(BaseCollector):
    name = "google_trends"
    min_interval_s = 1.0

    def ready(self) -> str | None:
        if not os.environ.get("SERPAPI_KEY"):
            return "SERPAPI_KEY not set"
        return None

    def _search(self, params: dict) -> dict:
        params = {**params, "api_key": os.environ["SERPAPI_KEY"], "engine": "google_trends"}
        resp = self.request("GET", SEARCH_URL, params=params)
        self.add_cost("search", 1, float(self.config.get("cost_per_search_usd", 0.01)))
        return resp.json()

    def fetch(self) -> list[dict]:
        items: list[dict] = []
        anchor = self.config.get("anchor_query", "weather")

        # 1) Rising related queries for each category seed — discovery of new entities
        for seed in self.config.get("seed_queries", []):
            data = self._search({"q": seed, "data_type": "RELATED_QUERIES"})
            rising = (data.get("related_queries") or {}).get("rising", [])
            if rising:
                items.append({
                    "external_id": f"rising:{seed}",
                    "item_date": None,
                    "payload": {"type": "rising_queries", "seed": seed, "rising": rising},
                })

        # 2) Interest-over-time for tracked entities, normalized against the anchor.
        #    Entity list comes from config for now; Phase 1 wires it to the entities table.
        for entity in self.config.get("tracked_entities", []):
            data = self._search({
                "q": f"{entity},{anchor}",
                "data_type": "TIMESERIES",
                "date": "today 3-m",
            })
            timeline = (data.get("interest_over_time") or {}).get("timeline_data", [])
            if timeline:
                items.append({
                    "external_id": f"interest:{entity}",
                    "item_date": None,
                    "payload": {
                        "type": "interest_over_time",
                        "entity": entity,
                        "anchor": anchor,
                        "timeline": timeline,
                    },
                })
        return items
