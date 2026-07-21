"""Classify existing entities as product / service / media / company / concept.

Only 'product' entities — things a shop could stock and sell — belong in the
daily report. Everything else stays for context but never headlines.

Usage: python -m scripts.classify_entities [--limit N] [--recheck]
"""

import argparse
import sys

from dotenv import load_dotenv

from pipeline import db, llm
from pipeline.entities import product_url

BATCH = 40

SYSTEM = """You classify consumer entities for a trend-discovery system whose owner
wants to spot PRODUCTS he could source, stock and sell before they peak.

For each name, return its kind:
  product — a physical thing a shop could stock and sell (levoit air purifier,
            owala freesip, dubai chocolate, play doh, blackout curtain)
  service — subscriptions, apps, platforms (amazon prime, apple music)
  media   — games, films, shows, characters, franchises (marvel rivals, naruto)
  company — chains, retailers, firms (popeyes, 7-eleven, costco)
  concept — techniques, routines, categories that are not one buyable item
            (double cleansing, whitening, humanoid robot, amazon finds)
  other   — anything else (people, places, events)

Be strict: if a shopper could not add it to a cart as one specific item, it is
not a product."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["product", "service", "media", "company",
                                      "concept", "other"]},
                },
                "required": ["name", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--recheck", action="store_true",
                        help="re-classify entities that already have a kind")
    args = parser.parse_args()

    conn = db.connect()
    db.apply_schema(conn)
    if llm.ready():
        raise SystemExit(llm.ready())

    where = "" if args.recheck else "and kind is null"
    rows = conn.execute(
        f"""select id, canonical_name from entities
            where status = 'active' {where}
            order by id limit %s""", (args.limit,)).fetchall()
    if not rows:
        print("nothing to classify")
        return 0

    counts: dict[str, int] = {}
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        by_name = {name: eid for eid, name in batch}
        result = llm.structured_call(
            conn, None, "entity_classification", llm.MODEL_EXTRACTION,
            SYSTEM, "\n".join(f"- {name}" for _, name in batch), SCHEMA,
            max_tokens=4000)
        for item in result.get("items", []):
            eid = by_name.get(item["name"])
            if not eid:
                continue
            kind = item["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            url = product_url(conn, item["name"]) if kind == "product" else None
            conn.execute(
                "update entities set kind = %s, product_url = coalesce(%s, product_url) where id = %s",
                (kind, url, eid))
        print(f"  classified {min(i + BATCH, len(rows))}/{len(rows)}")

    print("\nresult:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
