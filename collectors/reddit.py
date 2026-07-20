"""Reddit collector (plan items 13, 65).

Two modes:
  OAuth API (preferred) — activates automatically once REDDIT_CLIENT_ID/SECRET
  exist (waiting on Reddit's app approval). Full data: scores, comments, text.

  RSS fallback (interim) — Reddit's public syndication feeds, no auth. Titles
  only, and heavily rate-limited, so we take a deterministic ROTATING SLICE of
  the subreddit list each day (whole list covered every few days) with 10s
  spacing. Modest, standard syndication use; replaced by the API on approval.
"""

import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from .base import BaseCollector, CollectorError

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
ATOM = {"a": "http://www.w3.org/2005/Atom"}

RSS_SLICE = 30          # subreddits per day in RSS mode
RSS_SPACING_S = 15.0    # unauthenticated rate limits are unforgiving


class RedditCollector(BaseCollector):
    name = "reddit"
    min_interval_s = 1.1  # OAuth free tier: 100 QPM — stay comfortably under

    def _has_creds(self) -> bool:
        return bool(os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET"))

    def ready(self) -> str | None:
        return None           # RSS fallback means we can always run

    def fetch(self) -> list[dict]:
        if self._has_creds():
            return self._fetch_api()
        self.log.info("no API credentials — RSS fallback mode (rotating slice)")
        return self._fetch_rss()

    # ---- OAuth API mode (full fidelity) ----

    def _token(self) -> str:
        resp = self._session.post(
            TOKEN_URL,
            auth=(os.environ["REDDIT_CLIENT_ID"], os.environ["REDDIT_CLIENT_SECRET"]),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _fetch_api(self) -> list[dict]:
        self._session.headers["Authorization"] = f"Bearer {self._token()}"
        listing = self.config.get("listing", "hot")
        limit = int(self.config.get("posts_per_subreddit", 25))
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
                    "external_id": post.get("name"),
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
            self.log.info("r/%s: api ok", sub)
        return items

    # ---- RSS fallback mode (titles only, rotating slice) ----

    def _fetch_rss(self) -> list[dict]:
        subs = self.config.get("subreddits", [])
        if not subs:
            return []
        slice_n = int(self.config.get("rss_slice", RSS_SLICE))
        offset = (date.today().toordinal() * slice_n) % len(subs)
        todays = [subs[(offset + i) % len(subs)] for i in range(min(slice_n, len(subs)))]
        self.log.info("rss slice: %d of %d subreddits (offset %d)", len(todays), len(subs), offset)

        old_interval, self.min_interval_s = self.min_interval_s, RSS_SPACING_S
        self.max_retries = 1     # a 429'd sub just waits for its next rotation day
        today = date.today().isoformat()
        items: list[dict] = []
        try:
            for sub in todays:
                try:
                    resp = self.request(
                        "GET", f"https://www.reddit.com/r/{sub}/hot.rss",
                        params={"limit": 25})
                    root = ET.fromstring(resp.content)
                except (CollectorError, ET.ParseError) as exc:
                    self.log.warning("rss r/%s failed: %s", sub, exc)
                    continue
                count = 0
                for entry in root.findall("a:entry", ATOM):
                    title = entry.findtext("a:title", namespaces=ATOM)
                    eid = entry.findtext("a:id", namespaces=ATOM)
                    link_el = entry.find("a:link", ATOM)
                    updated = entry.findtext("a:updated", namespaces=ATOM) or ""
                    if not title:
                        continue
                    items.append({
                        "external_id": eid or f"{sub}:{title[:60]}",
                        "item_date": updated[:10] or today,
                        "payload": {
                            "subreddit": sub,
                            "title": title,
                            "selftext": "",
                            "score": None,            # not available via RSS
                            "permalink": (link_el.get("href", "") if link_el is not None else "")
                                         .replace("https://www.reddit.com", ""),
                            "mode": "rss",
                        },
                    })
                    count += 1
                self.log.info("rss r/%s: %d posts", sub, count)
        finally:
            self.min_interval_s = old_interval
        return items
