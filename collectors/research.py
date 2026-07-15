"""Research-publication collector (plan item 36). Free, RSS-based.

Watches the public feeds of research houses (ARK, CB Insights, McKinsey —
Gartner blocks bots). New posts flow into the normal extraction pipeline, so
products/trends named in institutional research become entities and signals
like any other source.
"""

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .base import BaseCollector, CollectorError

TAG_RE = re.compile(r"<[^>]+>")
ATOM = "{http://www.w3.org/2005/Atom}"


def _clean(text: str | None, limit: int = 800) -> str:
    return TAG_RE.sub(" ", text or "").replace("&nbsp;", " ").strip()[:limit]


def _parse_date(raw: str | None):
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date()          # RFC 822 (RSS)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()  # ISO (Atom)
    except ValueError:
        return None


class ResearchCollector(BaseCollector):
    name = "research"
    min_interval_s = 2.0
    max_retries = 2

    def fetch(self) -> list[dict]:
        lookback = int(self.config.get("lookback_days", 14))
        cutoff = date.today() - timedelta(days=lookback)
        max_per_feed = int(self.config.get("max_per_feed", 15))

        items: list[dict] = []
        for feed in self.config.get("feeds", []):
            try:
                resp = self.request("GET", feed["url"])
                root = ET.fromstring(resp.content)
            except (CollectorError, ET.ParseError) as exc:
                self.log.warning("feed %s failed: %s", feed.get("name"), exc)
                continue
            posts = root.iter("item")
            entries = list(posts) or list(root.iter(f"{ATOM}entry"))
            count = 0
            for post in entries:
                if count >= max_per_feed:
                    break
                title = _clean(post.findtext("title") or post.findtext(f"{ATOM}title"), 300)
                link = (post.findtext("link") or "").strip()
                if not link:                                # Atom: link is an attribute
                    el = post.find(f"{ATOM}link")
                    link = el.get("href", "") if el is not None else ""
                pub = _parse_date(post.findtext("pubDate")
                                  or post.findtext(f"{ATOM}updated")
                                  or post.findtext(f"{ATOM}published"))
                if not title or (pub and pub < cutoff):
                    continue
                summary = _clean(post.findtext("description") or post.findtext(f"{ATOM}summary"))
                items.append({
                    "external_id": link or f"{feed['name']}:{title[:80]}",
                    "item_date": (pub or date.today()).isoformat(),
                    "payload": {
                        "type": "research_post",
                        "publisher": feed["name"],
                        "title": title,
                        "link": link,
                        "summary": summary,
                    },
                })
                count += 1
            self.log.info("%s: %d recent posts", feed.get("name"), count)
        return items
