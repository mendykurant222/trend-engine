"""YouTube collector (plan item 38). Free — Data API v3, 10,000 quota units/day.

Entity-driven like GDELT/EDGAR: for each watched entity we pull the videos
published in the last N days that mention it. Daily video-upload volume becomes
the 'mentions' signal (creator attention is a leading indicator — creators
chase trends before audiences do).

Quota: each search costs 100 units → default cap 30 entities/run = 3,000
units, well under the free 10,000/day.
"""

import os
from datetime import date, datetime, timedelta, timezone

from .base import BaseCollector, CollectorError

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


class YouTubeCollector(BaseCollector):
    name = "youtube"
    min_interval_s = 0.5
    needs_watchlist = True
    watch_entities: list[tuple[int, str]] = []

    def ready(self) -> str | None:
        if not os.environ.get("YOUTUBE_API_KEY"):
            return "YOUTUBE_API_KEY not set"
        return None

    def fetch(self) -> list[dict]:
        lookback = int(self.config.get("lookback_days", 14))
        max_q = int(self.config.get("max_queries", 30))
        published_after = (datetime.now(timezone.utc) - timedelta(days=lookback)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        today = date.today().isoformat()

        items: list[dict] = []
        for entity_id, name in self.watch_entities[:max_q]:
            try:
                resp = self.request("GET", SEARCH_URL, params={
                    "key": os.environ["YOUTUBE_API_KEY"],
                    "part": "snippet",
                    "q": f'"{name}"',
                    "type": "video",
                    "order": "date",
                    "publishedAfter": published_after,
                    "maxResults": 50,
                })
            except CollectorError as exc:
                self.log.warning("youtube %r failed: %s", name, exc)
                continue
            self.add_cost("search", 100, 0.0)   # quota units, $0 — visibility only
            videos = []
            for hit in resp.json().get("items", []):
                snippet = hit.get("snippet", {})
                videos.append({
                    "videoId": (hit.get("id") or {}).get("videoId"),
                    "publishedAt": (snippet.get("publishedAt") or "")[:10],
                    "title": snippet.get("title"),
                    "channel": snippet.get("channelTitle"),
                })
            if videos:
                items.append({
                    "external_id": f"videos:{name}:{today}",
                    "item_date": today,
                    "payload": {
                        "type": "video_volume",
                        "entity_id": entity_id,
                        "entity": name,
                        "lookback_days": lookback,
                        "videos": videos,
                    },
                })
            self.log.info("youtube %r: %d recent videos", name, len(videos))
        return items
