"""Entity extraction and resolution (plan item 16).

Claude Haiku reads batches of raw texts and names the products/brands/trends
mentioned. Resolution maps each mention to a canonical entity via the alias
table; unknown mentions create new entities. Every merge is reversible via
entity_merges.

Phase 2 refinement (TODO): embeddings to catch near-duplicate aliases
("pool lamp" vs "pool light") with Claude adjudicating merges.
"""

import logging
import re

log = logging.getLogger("entities")

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer",
                              "description": "index of the source text this mention came from"},
                    "mentions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string",
                                         "description": "canonical English name, lowercase, singular"},
                                "category": {"type": "string"},
                            },
                            "required": ["name", "category"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["index", "mentions"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You identify specific consumer products, brands, and product trends \
mentioned in social media texts, for a trend-tracking system.

Rules:
- Only SPECIFIC products, brands, or nameable product trends (e.g. "stanley tumbler", \
"dubai chocolate", "cold plunge tub"). NOT generic nouns ("water bottle", "chocolate", "shoes").
- NEVER return a public corporation or mega-brand by itself ("apple", "tesla", "meta", \
"nike", "walmart") — corporations are not consumer trends. A specific product, model, or \
line IS valid ("tesla model y", "nike vaporfly", "apple vision pro").
- canonical name: English, lowercase, singular, no brand suffixes like "tm".
- category: one of consumer-products, gaming, fashion, home, gadgets, food-beverage, wellness, toys.
- A text with no specific product/brand/trend mentions gets an empty mentions list.
- Merge obvious variants within the batch (e.g. "Stanley cup" and "stanley tumblers" -> "stanley tumbler")."""

BATCH_SIZE = 25  # texts per Haiku call


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


# fuzzy alias resolution thresholds (plan item 54)
FUZZY_AUTO = 0.75      # trigram similarity above this merges automatically
FUZZY_MIN = 0.55       # between MIN and AUTO, Haiku adjudicates

MERGE_SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "boolean"}},
    "required": ["same"], "additionalProperties": False,
}

# evergreen noise never worth an LLM look or an entity (plan item 62)
EVERGREEN = {
    "fyp", "#fyp", "foryou", "foryoupage", "viral", "trending", "funny",
    "love", "video", "tiktok", "shorts", "memes", "asmr", "satisfying",
    "aesthetic", "diy", "lifehack", "amazon", "walmart", "haul",
}


def _fuzzy_candidate(conn, alias: str):
    return conn.execute(
        """select e.id, a.alias, similarity(a.alias, %s) as sim
           from entity_aliases a
           join entities e on e.id = a.entity_id and e.status = 'active'
           where similarity(a.alias, %s) >= %s
           order by sim desc limit 1""",
        (alias, alias, FUZZY_MIN),
    ).fetchone()


def resolve_mention(conn, name: str, category: str | None, run_id: int | None = None) -> int | None:
    """Alias -> entity_id. Exact match first, then trigram-fuzzy (auto-merge
    above 0.75 similarity, Haiku verdict between 0.55-0.75), else a new
    entity. Returns None for evergreen noise."""
    from pipeline import llm

    alias = normalize(name)
    if alias in EVERGREEN or len(alias) < 3:
        return None
    row = conn.execute(
        """select e.id from entity_aliases a
           join entities e on e.id = a.entity_id and e.status = 'active'
           where a.alias = %s""", (alias,),
    ).fetchone()
    if row:
        return row[0]

    cand = _fuzzy_candidate(conn, alias)
    if cand:
        eid, existing, sim = cand
        merge, decided = sim >= FUZZY_AUTO, f"trgm:{sim:.2f}"
        if not merge and llm.ready() is None:
            verdict = llm.structured_call(
                conn, run_id, "alias_merge", llm.MODEL_EXTRACTION,
                "You decide whether two short names refer to the SAME specific "
                "product, brand, or consumer trend. Spelling variants and "
                "singular/plural are the same; different products are not.",
                f"A: {alias}\nB: {existing}", MERGE_SCHEMA, max_tokens=50)
            merge, decided = bool(verdict.get("same")), f"haiku+trgm:{sim:.2f}"
        if merge:
            conn.execute(
                "insert into entity_aliases (entity_id, alias, source) values (%s, %s, %s) on conflict (alias) do nothing",
                (eid, alias, decided))
            log.info("fuzzy merge: %r -> %r (%s)", alias, existing, decided)
            return eid

    row = conn.execute(
        """insert into entities (canonical_name, category)
           values (%s, %s)
           on conflict (canonical_name) do update set category = coalesce(entities.category, excluded.category)
           returning id""",
        (alias, category),
    ).fetchone()
    entity_id = row[0]
    conn.execute(
        "insert into entity_aliases (entity_id, alias, source) values (%s, %s, %s) on conflict (alias) do nothing",
        (entity_id, alias, "extraction"),
    )
    return entity_id


def link(conn, raw_item_id: int, entity_id: int) -> None:
    conn.execute(
        "insert into raw_item_entities (raw_item_id, entity_id) values (%s, %s) on conflict do nothing",
        (raw_item_id, entity_id),
    )


def item_text(source: str, payload: dict) -> str | None:
    """Extract the analyzable text from a raw item, per source."""
    if source == "reddit" or source == "_synthetic":
        title = payload.get("title") or ""
        body = (payload.get("selftext") or "")[:500]
        return f"{title}\n{body}".strip() or None
    if source == "google_trends" and payload.get("type") == "rising_query":
        return f"Rising Google search: {payload.get('query')} ({payload.get('value')})"
    if source == "google_trends" and payload.get("type") == "rising_queries":  # legacy items
        queries = [r.get("query", "") for r in payload.get("rising", [])]
        return "Rising search queries: " + ", ".join(q for q in queries if q)
    if source == "amazon" and payload.get("type") == "bestseller_titles":
        tops = payload.get("titles", [])[:10]
        return (f"Amazon best sellers (category {payload.get('category')}): "
                + "; ".join((t.get("title") or "")[:80] for t in tops))
    if source == "research" and payload.get("type") == "research_post":
        return f"{payload.get('publisher')}: {payload.get('title')}\n{(payload.get('summary') or '')[:500]}"
    if source == "tiktok" and payload.get("type") == "creative_center":
        row = payload.get("row", {})
        tag = normalize(str(row.get("name") or "").lstrip("#"))
        if tag in EVERGREEN:                      # plan item 62 — skip before Haiku
            return None
        return (f"TikTok trending {row.get('type', 'hashtag')}: {row.get('name')} "
                f"(views {row.get('videoViews')}, trend {row.get('trend')})")
    # amazon/tiktok payloads are structured (ASINs, hashtag rows) — Phase 1 TODO:
    # resolve ASINs to titles via Keepa before extraction
    return None


def run_extraction(conn, run_id: int | None, limit: int = 500) -> dict:
    """Process raw items that haven't had entities extracted yet."""
    from pipeline import llm

    reason = llm.ready()
    if reason:
        log.warning("extraction skipped: %s", reason)
        return {"status": "skipped", "reason": reason}

    rows = conn.execute(
        """select id, source, payload from raw_items
           where entities_extracted_at is null
           order by id limit %s""", (limit,),
    ).fetchall()
    if not rows:
        return {"status": "ok", "items": 0, "mentions": 0}

    mentions_total = 0
    processed_ids = []
    batch: list[tuple[int, str]] = []  # (raw_item_id, text)

    def flush(batch):
        nonlocal mentions_total
        if not batch:
            return
        numbered = "\n\n".join(f"[{i}] {text[:800]}" for i, (_, text) in enumerate(batch))
        result = llm.structured_call(
            conn, run_id, "entity_extraction", llm.MODEL_EXTRACTION,
            SYSTEM_PROMPT, numbered, EXTRACTION_SCHEMA,
        )
        for item in result.get("items", []):
            idx = item.get("index")
            if idx is None or not (0 <= idx < len(batch)):
                continue
            raw_item_id = batch[idx][0]
            for mention in item.get("mentions", []):
                entity_id = resolve_mention(conn, mention["name"], mention.get("category"), run_id)
                if entity_id is None:
                    continue
                link(conn, raw_item_id, entity_id)
                mentions_total += 1

    for item_id, source, payload in rows:
        processed_ids.append(item_id)
        text = item_text(source, payload)
        if text:
            batch.append((item_id, text))
        if len(batch) >= BATCH_SIZE:
            flush(batch)
            batch = []
    flush(batch)

    conn.execute(
        "update raw_items set entities_extracted_at = now() where id = any(%s)",
        (processed_ids,),
    )
    log.info("extraction: %d items, %d mentions", len(processed_ids), mentions_total)
    return {"status": "ok", "items": len(processed_ids), "mentions": mentions_total}
