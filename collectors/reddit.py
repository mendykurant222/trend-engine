"""Reddit collector (plan item 13). Free official API via OAuth client-credentials.

Needs REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET (create a "script" app at
https://www.reddit.com/prefs/apps).
"""

import os
from datetime import datetime, timezone

from .base import BaseCollector, CollectorError

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"


class RedditCollector(BaseCollector):
    name = "reddit"
    min_interval_s = 1.1  # free tier: 100 QPM — stay comfortably under

    def ready(self) -> str | None:
        if not (os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")):
            return "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set"
        return None

    def _token(self) -> str:
        resp = self._session.post(
            TOKEN_URL,
            auth=(os.environ["REDDIT_CLIENT_ID"], os.environ["REDDIT_CLIENT_SECRET"]),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def fetch(self) -> list[dict]:
        self._session.headers["Authorization"] = f"Bearer {self._token()}"
        listing = self.config.get("listing", "hot")
        limit = int(self.config.get("posts_per_subreddit", 50))
        items: list[dict] = []
        for sub in self.config.get("subreddits", []):
            try:
                resp = self.request("GET", f"{API}/r/{sub}/{listing}", params={"limit": limit})
            except CollectorError as exc:
                self.log.warning("subreddit %s failed: %s", sub, exc)
                continue
            for child in resp.json().get("data", {}).get("children", []):
                post = child.get("data", {})
                created = datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc)
                items.append({
                    "external_id": post.get("name"),  # t3_xxxx
                    "item_date": created.date().isoformat(),
                    "payload": {
                        "subreddit": sub,
                        "id": post.get("id"),
                        "title": post.get("title"),
                        "selftext": (post.get("selftext") or "")[:2000],
                        "score": post.get("score"),
                        "num_comments": post.get("num_comments"),
                        "upvote_ratio": post.get("upvote_ratio"),
                        "created_utc": post.get("created_utc"),
                        "permalink": post.get("permalink"),
                        "url": post.get("url"),
                    },
                })
            self.log.info("r/%s: %d posts", sub, limit)
        return items
