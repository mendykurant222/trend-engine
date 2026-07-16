"""ASIN → title resolution (plan item 77).

Keepa best-seller lists arrive as bare ASIN codes. This step resolves the top
N per category to product titles (Keepa product API, batched, cached in
asin_titles) and stores a 'bestseller_titles' raw item that entity extraction
can actually read. Because best-seller lists barely change day to day, the
cache makes the marginal token cost near zero after the first run.
"""

import logging
import os
from datetime import date

import requests

from pipeline import db

log = logging.getLogger("asin_titles")

KEEPA_PRODUCT = "https://api.keepa.com/product"
TOP_N = 20


def resolve_asin_titles(conn, config: dict, run_id: int | None = None) -> dict:
    key = os.environ.get("KEEPA_KEY")
    if not key:
        return {"status": "skipped", "reason": "KEEPA_KEY not set"}
    acfg = config.get("sources", {}).get("amazon", {})
    domain = int(acfg.get("domain", 1))
    cost_per_token = float(acfg.get("cost_per_token_usd", 0.0005))

    rows = conn.execute(
        """select payload from raw_items
           where source = 'amazon' and payload->>'type' = 'bestsellers'
             and collected_at >= now() - interval '2 days'"""
    ).fetchall()
    if not rows:
        return {"status": "ok", "categories": 0, "resolved": 0, "stored": 0}

    per_cat: dict = {}
    for (payload,) in rows:
        per_cat[payload["category"]] = payload.get("asins", [])[:TOP_N]

    wanted = sorted({a for asins in per_cat.values() for a in asins})
    cached = dict(conn.execute(
        "select asin, title from asin_titles where asin = any(%s)", (wanted,)
    ).fetchall())
    missing = [a for a in wanted if a not in cached]

    resolved = 0
    for i in range(0, len(missing), 100):
        batch = missing[i:i + 100]
        resp = requests.get(KEEPA_PRODUCT, params={
            "key": key, "domain": domain, "asin": ",".join(batch), "history": 0,
        }, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        tokens = data.get("tokensConsumed", len(batch))
        db.record_cost(conn, run_id, "amazon", "product_titles", tokens, tokens * cost_per_token)
        for product in data.get("products", []):
            asin, title = product.get("asin"), product.get("title")
            if asin and title:
                conn.execute(
                    """insert into asin_titles (asin, title) values (%s, %s)
                       on conflict (asin) do update set title = excluded.title, updated_at = now()""",
                    (asin, title))
                cached[asin] = title
                resolved += 1

    today = date.today().isoformat()
    stored = 0
    for cat, asins in per_cat.items():
        titles = [{"rank": i + 1, "asin": a, "title": cached[a]}
                  for i, a in enumerate(asins) if a in cached]
        if titles:
            stored += db.store_raw_items(conn, "amazon", [{
                "external_id": f"titles:{cat}:{today}",
                "item_date": today,
                "payload": {"type": "bestseller_titles", "category": cat, "titles": titles},
            }])
    log.info("asin titles: %d categories, %d newly resolved, %d title items stored",
             len(per_cat), resolved, stored)
    return {"status": "ok", "categories": len(per_cat), "resolved": resolved, "stored": stored}
