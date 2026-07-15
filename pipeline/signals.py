"""Signals Builder (plan item 17): turn linked raw items into daily per-entity
metrics. Upserts are idempotent — re-running a day recomputes it.

Metrics v1:
  mentions   — count of raw items linked to the entity, per source per day
  score_sum  — reddit only: sum of post scores (weight by traction, not just count)

The min-mentions threshold (config signals.min_mentions_threshold) applies at
the ANOMALY stage, not here — signals store everything so thresholds can be
re-tuned without recollecting.
"""

import logging

log = logging.getLogger("signals")


def build_daily_signals(conn, signal_date=None) -> int:
    """Build/refresh daily signals. signal_date=None processes all dates."""
    date_filter = "and ri.item_date = %(d)s" if signal_date else ""
    params = {"d": signal_date}

    cur = conn.execute(f"""
        insert into daily_signals (entity_id, source, signal_date, metric, value)
        select rie.entity_id, ri.source, ri.item_date, 'mentions', count(*)
        from raw_item_entities rie
        join raw_items ri on ri.id = rie.raw_item_id
        where ri.item_date is not null {date_filter}
        group by 1, 2, 3
        on conflict (entity_id, source, signal_date, metric)
        do update set value = excluded.value
    """, params)
    n = cur.rowcount

    cur = conn.execute(f"""
        insert into daily_signals (entity_id, source, signal_date, metric, value)
        select rie.entity_id, ri.source, ri.item_date, 'score_sum',
               sum(coalesce((ri.payload->>'score')::numeric, 0))
        from raw_item_entities rie
        join raw_items ri on ri.id = rie.raw_item_id
        where ri.item_date is not null
          and ri.source in ('reddit', '_synthetic')
          {date_filter}
        group by 1, 2, 3
        on conflict (entity_id, source, signal_date, metric)
        do update set value = excluded.value
    """, params)
    n += cur.rowcount

    log.info("signals: %d rows upserted", n)
    return n
