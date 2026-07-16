"""TikTok benchmark check (plan item 56).

TikTok Creative Center's own ranked list is ground truth: if a top-10 hashtag
maps to a tracked entity and our detector did NOT flag it today, that's a
"missed benchmark" — free daily calibration signal, reported in the summary.
"""

import logging
from datetime import date

log = logging.getLogger("benchmark")


def tiktok_benchmark_check(conn, target_date: date | None = None, top_n: int = 10) -> list[str]:
    target_date = target_date or date.today()
    rows = conn.execute(
        """select ri.id, ri.payload->'row'->>'name',
                  (ri.payload->'row'->>'rank')::int
           from raw_items ri
           where ri.source = 'tiktok' and ri.item_date = %s
             and (ri.payload->'row'->>'rank')::int <= %s""",
        (target_date, top_n),
    ).fetchall()
    missed = []
    for raw_id, tag, rank in rows:
        entity_ids = [r[0] for r in conn.execute(
            "select entity_id from raw_item_entities where raw_item_id = %s", (raw_id,)
        ).fetchall()]
        if not entity_ids:        # evergreen-filtered or not yet extracted
            continue
        flagged = conn.execute(
            "select 1 from anomalies where signal_date = %s and entity_id = any(%s) limit 1",
            (target_date, entity_ids),
        ).fetchone()
        if not flagged:
            missed.append(f"{tag} (#{rank})")
    if missed:
        log.info("benchmark: %d top-%d tiktok tags not flagged: %s",
                 len(missed), top_n, ", ".join(missed))
    return missed
