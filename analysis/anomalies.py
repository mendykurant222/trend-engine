"""Anomaly detection (plan items 18-21).

Three detectors over daily_signals (metric='mentions'):

  surge         — today's mentions vs a same-weekday baseline (day-of-week
                  seasonality: Saturday compares to past Saturdays), scored by
                  Poisson tail probability instead of a naive z-score — sparse
                  count data (0-2 mentions/day) breaks z-scores.
  acceleration  — velocity (7-day mean) vs the prior 7-day mean; catches
                  "how fast it's changing", the heart of early detection.
  new_entity    — entity first seen within the window, with enough mentions
                  across enough distinct sources (the "pool lamp" case).

Cross-source bonus: an entity anomalous in 2+ sources within a 14-day window
(not simultaneity — sources lag each other) gets its score multiplied.
TODO Phase 2 tuning: extra bonus for the expected sequence tiktok→reddit→amazon.

Idempotent per date: recomputing a date deletes and rebuilds its anomalies.
Scores are 0-100 (-10*log10(p) for surges, capped).
"""

import logging
import math
from datetime import date, timedelta

from psycopg.types.json import Jsonb

log = logging.getLogger("anomalies")

HISTORY_DAYS = 90


def poisson_sf(k: float, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam). Normal approximation above lam=30 —
    news/interest volumes run in the hundreds, where the exact sum is both
    slow and inappropriate (overdispersion)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    if lam > 30:
        z = (k - 0.5 - lam) / math.sqrt(lam)
        return max(0.0, min(1.0, 0.5 * math.erfc(z / math.sqrt(2))))
    cdf = 0.0
    for i in range(int(k)):
        cdf += math.exp(i * math.log(lam) - lam - math.lgamma(i + 1))
    return max(0.0, min(1.0, 1.0 - cdf))


def _surge_score(p: float) -> int:
    if p <= 0:
        return 100
    return min(100, round(-10 * math.log10(p)))


def _load_history(conn, target_date: date):
    """All mention signals in the window + each entity's first_seen."""
    since = target_date - timedelta(days=HISTORY_DAYS)
    series: dict[tuple[int, str], dict[date, float]] = {}
    for eid, source, d, value in conn.execute(
        """select entity_id, source, signal_date, value from daily_signals
           where metric = 'mentions' and signal_date between %s and %s""",
        (since, target_date),
    ).fetchall():
        series.setdefault((eid, source), {})[d] = float(value)
    first_seen = dict(conn.execute(
        "select id, first_seen from entities where status = 'active'").fetchall())
    return series, first_seen


def detect_anomalies(conn, config: dict, target_date: date | None = None) -> int:
    acfg = config.get("anomalies", {})
    min_mentions = config.get("signals", {}).get("min_mentions_threshold", 3)
    p_threshold = float(acfg.get("poisson_p_threshold", 0.001))
    accel_ratio = float(acfg.get("acceleration_ratio", 2.0))
    ne_window = int(acfg.get("new_entity_window_days", 7))
    ne_min_mentions = int(acfg.get("new_entity_min_mentions", 5))
    ne_min_sources = int(acfg.get("new_entity_min_sources", 2))
    xs_window = int(acfg.get("cross_source_window_days", 14))
    xs_bonus = float(acfg.get("cross_source_bonus", 2.0))
    weights = acfg.get("source_weights", {}) or {}          # plan item 40
    target_date = target_date or date.today()

    conn.execute("delete from anomalies where signal_date = %s", (target_date,))
    series, first_seen = _load_history(conn, target_date)

    found = []  # (entity_id, source, kind, score, details)

    for (eid, source), s in series.items():
        today = s.get(target_date, 0)
        seen = first_seen.get(eid)
        if seen is None or today < min_mentions:
            continue
        age_days = (target_date - seen).days

        # ---- surge: same-weekday Poisson baseline ----
        samples = [s.get(target_date - timedelta(days=7 * k), 0.0)
                   for k in range(1, 9)
                   if target_date - timedelta(days=7 * k) >= seen]
        if len(samples) < 3 and age_days >= 14:
            # young-ish entity: fall back to trailing 28-day mean
            days = [target_date - timedelta(days=i) for i in range(1, 29)]
            samples = [s.get(d, 0.0) for d in days if d >= seen]
        if len(samples) >= 3:
            lam = max(0.1, sum(samples) / len(samples))
            p = poisson_sf(today, lam)
            if p < p_threshold:
                found.append((eid, source, "surge", _surge_score(p), {
                    "today": today, "baseline_mean": round(lam, 2),
                    "baseline_samples": len(samples), "p": p,
                }))

        # ---- acceleration: velocity now vs prior week ----
        if age_days >= 14:
            v_now = sum(s.get(target_date - timedelta(days=i), 0.0) for i in range(7)) / 7
            v_prev = sum(s.get(target_date - timedelta(days=i), 0.0) for i in range(7, 14)) / 7
            if v_prev >= 0.5 and v_now / v_prev >= accel_ratio:
                score = min(100, round(30 + 20 * (v_now / v_prev - accel_ratio)))
                found.append((eid, source, "acceleration", score, {
                    "velocity_now": round(v_now, 2), "velocity_prev": round(v_prev, 2),
                    "ratio": round(v_now / v_prev, 2),
                }))

    # ---- new entity surge: cross-source by construction ----
    window_start = target_date - timedelta(days=ne_window)
    for eid, seen in first_seen.items():
        if seen < window_start:
            continue
        total, sources = 0.0, set()
        for (e2, source), s in series.items():
            if e2 != eid:
                continue
            in_window = sum(v for d, v in s.items() if d >= window_start)
            if in_window > 0:
                total += in_window
                sources.add(source)
        if total >= ne_min_mentions and len(sources) >= ne_min_sources:
            score = min(100, round(40 + 5 * total))
            found.append((eid, "multi", "new_entity", score, {
                "mentions_in_window": total, "sources": sorted(sources),
                "first_seen": str(seen),
            }))

    for eid, source, kind, score, details in found:
        weight = float(weights.get(source, 1.0))
        if weight != 1.0 and source != "multi":
            score = max(1, min(100, round(score * weight)))
            details = {**details, "source_weight": weight}
        conn.execute(
            """insert into anomalies (entity_id, source, signal_date, kind, score, details)
               values (%s, %s, %s, %s, %s, %s)""",
            (eid, source, target_date, kind, score, Jsonb(details)),
        )

    # ---- cross-source bonus over the window (plan item 21) ----
    boosted = conn.execute(
        """with multi as (
             select entity_id from anomalies
             where signal_date between %s and %s and source != 'multi'
             group by entity_id having count(distinct source) >= 2
           )
           update anomalies a
           set score = least(100, round(a.score * %s)),
               details = a.details || '{"cross_source": true}'::jsonb
           where a.signal_date = %s and a.entity_id in (select entity_id from multi)
             and not (a.details ? 'cross_source')
           returning a.entity_id""",
        (target_date - timedelta(days=xs_window), target_date, xs_bonus, target_date),
    ).fetchall()

    # ---- sequence bonus (plan item 61): leading sources firing BEFORE
    # lagging ones is the expected shape of a real trend ----
    seq_bonus = float(acfg.get("sequence_bonus", 1.25))
    leading = {"tiktok", "google_trends", "google_trends_interest",
               "youtube", "reddit", "_synthetic"}
    lagging = {"amazon", "sec_edgar", "research", "gdelt", "_synthetic2"}
    first_by_src: dict[int, dict[str, date]] = {}
    for eid, src, d in conn.execute(
        """select entity_id, source, min(signal_date) from anomalies
           where signal_date between %s and %s and source != 'multi'
           group by 1, 2""",
        (target_date - timedelta(days=xs_window), target_date),
    ).fetchall():
        first_by_src.setdefault(eid, {})[src] = d
    seq_entities = []
    for eid, srcs in first_by_src.items():
        lead = min((d for s, d in srcs.items() if s in leading), default=None)
        lag = min((d for s, d in srcs.items() if s in lagging), default=None)
        if lead and lag and lead < lag:
            seq_entities.append(eid)
    if seq_entities:
        conn.execute(
            """update anomalies
               set score = least(100, round(score * %s)),
                   details = details || '{"sequence": true}'::jsonb
               where signal_date = %s and entity_id = any(%s)
                 and not (details ? 'sequence')""",
            (seq_bonus, target_date, seq_entities))

    log.info("anomalies %s: %d found, %d cross-source boosted, %d sequence-boosted",
             target_date, len(found), len(boosted), len(seq_entities))
    return len(found)


def debug_report(conn, target_date: date | None = None, top_n: int = 10) -> list[str]:
    """Daily calibration lines for the run summary (plan item 22)."""
    target_date = target_date or date.today()
    rows = conn.execute(
        """select e.canonical_name, a.source, a.kind, a.score, a.details
           from anomalies a join entities e on e.id = a.entity_id
           where a.signal_date = %s order by a.score desc limit %s""",
        (target_date, top_n),
    ).fetchall()
    if not rows:
        return [f"Anomalies ({target_date}): none"]
    lines = [f"Anomalies ({target_date}), top {len(rows)}:"]
    for name, source, kind, score, details in rows:
        extra = " ×2 cross-source" if details.get("cross_source") else ""
        if kind == "surge":
            desc = f"{details['today']:.0f} vs baseline {details['baseline_mean']}"
        elif kind == "acceleration":
            desc = f"velocity {details['velocity_prev']}→{details['velocity_now']}/day"
        else:
            desc = f"{details['mentions_in_window']:.0f} mentions, {len(details['sources'])} sources"
        lines.append(f"  [{score:>3.0f}] {name} ({source}, {kind}): {desc}{extra}")
    return lines
