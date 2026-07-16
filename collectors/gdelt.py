"""GDELT news collector (plan item 37). Free, no key.

For each watched entity we pull the DOC 2.0 raw volume timeline — daily
article counts over the last ~90 days in ONE call. That is instant historical
backfill for the news source: same-weekday baselines exist from the first run.

The watchlist is injected by the orchestrator.
"""

import re
from datetime import date

from .base import BaseCollector, CollectorError

DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _parse_gdelt_date(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")[:8]
    if len(digits) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


class GdeltCollector(BaseCollector):
    name = "gdelt"
    min_interval_s = 3.0           # GDELT rate-limits aggressively
    max_retries = 2
    needs_watchlist = True
    watch_entities: list[tuple[int, str]] = []

    def fetch(self) -> list[dict]:
        import time as _time
        timespan = int(self.config.get("timespan_days", 90))
        max_q = int(self.config.get("max_queries", 20))
        budget_s = int(self.config.get("fetch_budget_s", 240))
        deadline = _time.monotonic() + budget_s
        today = date.today().isoformat()

        items: list[dict] = []
        for i, (entity_id, name) in enumerate(self.watch_entities[:max_q]):
            if _time.monotonic() > deadline:
                self.log.warning("fetch budget (%ds) exhausted — %d of %d entities skipped",
                                 budget_s, min(max_q, len(self.watch_entities)) - i, max_q)
                break
            try:
                resp = self.request("GET", DOC_URL, params={
                    "query": f'"{name}"',
                    "mode": "timelinevolraw",
                    "format": "json",
                    "timespan": f"{timespan}d",
                })
                data = resp.json()
            except (CollectorError, ValueError) as exc:
                self.log.warning("gdelt %r failed: %s", name, exc)
                continue
            series = (data.get("timeline") or [{}])[0].get("data", [])
            timeline = []
            for point in series:
                d = _parse_gdelt_date(point.get("date", ""))
                if d:
                    timeline.append({"date": d, "value": float(point.get("value") or 0)})
            if timeline:
                items.append({
                    "external_id": f"vol:{name}:{today}",
                    "item_date": today,
                    "payload": {
                        "type": "news_volume",
                        "entity_id": entity_id,
                        "entity": name,
                        "timespan_days": timespan,
                        "timeline": timeline,
                    },
                })
            self.log.info("gdelt %r: %d timeline points", name, len(timeline))
        return items
