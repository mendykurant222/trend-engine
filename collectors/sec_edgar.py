"""SEC EDGAR collector (plan item 35). Free, official, no key.

Note: EDGAR does not host earnings-call transcripts (those are third-party,
paywalled). What it has is FULL-TEXT SEARCH across filings — 8-K earnings
releases, 10-K/10-Q. We search our tracked entities and get back the exact
companies (with CIKs) discussing them in official filings: a direct
company↔trend link with a verified source.

The watchlist (entities worth searching) is injected by the orchestrator.
"""

import os
import re
from datetime import date, timedelta

from .base import BaseCollector, CollectorError

FTS_URL = "https://efts.sec.gov/LATEST/search-index"


class SecEdgarCollector(BaseCollector):
    name = "sec_edgar"
    min_interval_s = 1.0            # SEC fair-use: stay well under 10 req/s
    needs_watchlist = True
    watch_entities: list[tuple[int, str]] = []

    def __init__(self, config):
        super().__init__(config)
        self._session.headers["User-Agent"] = (
            "trend-engine personal research " + os.environ.get("SEC_CONTACT", "mkurant@bkonect.com"))

    def fetch(self) -> list[dict]:
        forms = ",".join(self.config.get("forms", ["8-K", "10-K", "10-Q"]))
        lookback = int(self.config.get("lookback_days", 30))
        max_q = int(self.config.get("max_queries", 20))
        start = (date.today() - timedelta(days=lookback)).isoformat()
        end = date.today().isoformat()

        items: list[dict] = []
        for entity_id, name in self.watch_entities[:max_q]:
            try:
                resp = self.request("GET", FTS_URL, params={
                    "q": f'"{name}"', "forms": forms,
                    "dateRange": "custom", "startdt": start, "enddt": end,
                })
            except CollectorError as exc:
                self.log.warning("edgar search %r failed: %s", name, exc)
                continue
            hits = resp.json().get("hits", {}).get("hits", [])
            for h in hits[:20]:
                src = h.get("_source", {})
                display = src.get("display_names") or []
                ticker = None
                if display:
                    m = re.search(r"\(([A-Z][A-Z0-9.\-]{0,9})\)", display[0])
                    ticker = m.group(1) if m else None
                items.append({
                    "external_id": f"{name}:{h.get('_id')}",
                    "item_date": (src.get("file_date") or "")[:10] or None,
                    "payload": {
                        "type": "filing_mention",
                        "entity_id": entity_id,
                        "entity": name,
                        "company": display[0] if display else None,
                        "ticker": ticker,
                        "cik": src.get("cik"),
                        "form": src.get("file_type") or src.get("form"),
                        "file_date": src.get("file_date"),
                        "accession": h.get("_id"),
                    },
                })
            self.log.info("edgar %r: %d filing hits", name, min(len(hits), 20))
        return items
