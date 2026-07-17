"""Google Trends via SerpApi — the PRIMARY path, pytrends dropped (plan item 12).

Needs SERPAPI_KEY. Every timeseries request includes the fixed anchor query so
values are comparable across days (Google returns relative 0-100 per request).
"""

import os
from datetime import date

from .base import BaseCollector

SEARCH_URL = "https://serpapi.com/search.json"


class GoogleTrendsCollector(BaseCollector):
    name = "google_trends"
    min_interval_s = 1.0
    needs_watchlist = True
    watch_entities: list[tuple[int, str]] = []

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

        # 1) Rising related queries — ONE raw item per query, so entities link
        #    precisely to the query that surfaced them and "Breakout" (+5000%,
        #    pre-peak by definition) can boost them downstream
        today = date.today().isoformat()
        for seed in self.config.get("seed_queries", []):
            data = self._search({"q": seed, "data_type": "RELATED_QUERIES"})
            rising = (data.get("related_queries") or {}).get("rising", [])
            for r in rising:
                query = (r.get("query") or "").strip()
                if not query:
                    continue
                items.append({
                    "external_id": f"rq:{seed}:{query}:{today}",
                    "item_date": today,
                    "payload": {"type": "rising_query", "seed": seed,
                                "query": query, "value": str(r.get("value", ""))},
                })
            self.log.info("seed %r: %d rising queries", seed, len(rising))

        # 2) Interest-over-time for watched entities (plan item 53). One call
        #    returns ~90 days of self-consistent daily values — like GDELT,
        #    the source back-fills its own baselines.
        items.extend(self.fetch_interest())
        return items

    def fetch_interest(self, entities: list[tuple[int, str]] | None = None) -> list[dict]:
        from datetime import datetime, timezone
        anchor = self.config.get("anchor_query", "weather")
        interest_max = int(self.config.get("interest_max", 25))
        today = date.today().isoformat()
        # the daily cap applies to watchlist runs; an explicit list (bulk
        # backfill) is taken in full
        pool = entities if entities is not None else self.watch_entities[:interest_max]
        items: list[dict] = []
        for entity_id, name in pool:
            data = self._search({
                "q": f"{name},{anchor}",
                "data_type": "TIMESERIES",
                "date": "today 3-m",
            })
            pts = []
            for p in (data.get("interest_over_time") or {}).get("timeline_data", []):
                ts, vals = p.get("timestamp"), p.get("values") or []
                v = vals[0].get("extracted_value") if vals else None
                if ts and v is not None:
                    d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
                    pts.append({"date": d, "value": float(v)})
            if pts:
                items.append({
                    "external_id": f"interest:{name}:{today}",
                    "item_date": today,
                    "payload": {"type": "interest_timeline", "entity_id": entity_id,
                                "entity": name, "anchor": anchor, "timeline": pts},
                })
            self.log.info("interest %r: %d points", name, len(pts))
        return items
