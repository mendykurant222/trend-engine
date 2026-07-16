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

    n += _structured_source_signals(conn)
    log.info("signals: %d rows upserted", n)
    return n


def _structured_source_signals(conn) -> int:
    """Sources whose items are already entity-linked at collection time
    (no extraction step): GDELT news volume timelines and EDGAR filing hits.
    Both land as metric='mentions' so anomaly detection and the cross-source
    bonus treat them like any other source."""
    # GDELT: each recent item carries a ~90-day daily-volume timeline —
    # upserting it back-fills history for the news source automatically.
    cur = conn.execute("""
        insert into daily_signals (entity_id, source, signal_date, metric, value)
        select distinct on ((ri.payload->>'entity_id')::bigint, (point->>'date')::date)
               (ri.payload->>'entity_id')::bigint, 'gdelt',
               (point->>'date')::date, 'mentions', (point->>'value')::numeric
        from raw_items ri, jsonb_array_elements(ri.payload->'timeline') point
        where ri.source = 'gdelt' and ri.payload->>'type' = 'news_volume'
          and ri.collected_at >= now() - interval '3 days'
          and exists (select 1 from entities e where e.id = (ri.payload->>'entity_id')::bigint)
        order by (ri.payload->>'entity_id')::bigint, (point->>'date')::date, ri.collected_at desc
        on conflict (entity_id, source, signal_date, metric)
        do update set value = excluded.value
    """)
    n = cur.rowcount

    # Google Trends interest timelines (plan item 53) — own source so the
    # 0-100 interest scale never mixes with rising-query counts
    cur = conn.execute("""
        insert into daily_signals (entity_id, source, signal_date, metric, value)
        select distinct on ((ri.payload->>'entity_id')::bigint, (point->>'date')::date)
               (ri.payload->>'entity_id')::bigint, 'google_trends_interest',
               (point->>'date')::date, 'mentions', (point->>'value')::numeric
        from raw_items ri, jsonb_array_elements(ri.payload->'timeline') point
        where ri.source = 'google_trends' and ri.payload->>'type' = 'interest_timeline'
          and ri.collected_at >= now() - interval '3 days'
          and exists (select 1 from entities e where e.id = (ri.payload->>'entity_id')::bigint)
        order by (ri.payload->>'entity_id')::bigint, (point->>'date')::date, ri.collected_at desc
        on conflict (entity_id, source, signal_date, metric)
        do update set value = excluded.value
    """)
    n += cur.rowcount

    # YouTube: daily video-upload volume per entity (creator attention)
    cur = conn.execute("""
        insert into daily_signals (entity_id, source, signal_date, metric, value)
        select (ri.payload->>'entity_id')::bigint, 'youtube',
               (video->>'publishedAt')::date, 'mentions', count(*)
        from raw_items ri, jsonb_array_elements(ri.payload->'videos') video
        where ri.source = 'youtube' and ri.payload->>'type' = 'video_volume'
          and ri.collected_at >= now() - interval '3 days'
          and video->>'publishedAt' != ''
          and exists (select 1 from entities e where e.id = (ri.payload->>'entity_id')::bigint)
        group by 1, 2, 3
        on conflict (entity_id, source, signal_date, metric)
        do update set value = excluded.value
    """)
    n += cur.rowcount

    cur = conn.execute("""
        insert into daily_signals (entity_id, source, signal_date, metric, value)
        select (ri.payload->>'entity_id')::bigint, 'sec_edgar',
               ri.item_date, 'mentions', count(*)
        from raw_items ri
        where ri.source = 'sec_edgar' and ri.payload->>'type' = 'filing_mention'
          and ri.item_date is not null
          and exists (select 1 from entities e where e.id = (ri.payload->>'entity_id')::bigint)
        group by 1, 2, 3
        on conflict (entity_id, source, signal_date, metric)
        do update set value = excluded.value
    """)
    return n + cur.rowcount
