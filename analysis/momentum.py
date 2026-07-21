"""Momentum scoring (added 21.07 — Mendy's catch).

The engine was ranking trends by STRENGTH, which is essentially size: how much
total noise a topic makes. That surfaces things at or past their peak — on real
data, 5 of the top 10 by volume were in decline (humanoid robot −64%, apple
music −74%). The goal is the opposite: catch things while they're still small
and climbing.

Momentum ranks by RATE OF CHANGE instead, with two guards against the classic
failure of growth ranking:

  smoothing — a jump from 1 to 20 mentions is +1900% but means nothing.
  Adding a constant to both sides of the ratio makes tiny bases boring.

  volume floor — below a few mentions a week, nothing is a trend yet;
  those entities get damped rather than topping the list.

Score is 0-100 around a neutral 50: 50 = flat, ~75 = doubling week over week,
100 = 8x or more, below 50 = declining. Multi-source agreement adds a bonus,
because one platform shouting is noise and three agreeing is a trend.
"""

import math

SMOOTHING = 8.0        # deadens tiny-base explosions in the ratio
VOLUME_HALF = 40.0     # weekly mentions at which a move counts at half weight
SOURCE_BONUS = 4       # per independent source beyond the first (max +12)


def momentum_score(last7: float, prev7: float, n_sources: int = 1) -> int:
    """0-100 momentum. 50 = flat, >50 rising, <50 declining.

    Two dampers keep small numbers honest: smoothing inside the ratio, and a
    volume weight that pulls low-volume moves back toward neutral. A jump from
    1 to 20 mentions is a big percentage and a small fact; it should not
    outrank a genuine doubling at real volume.
    """
    ratio = (last7 + SMOOTHING) / (prev7 + SMOOTHING)
    raw = 50 + 25 * math.log2(ratio)       # doubling = +25, 8x = +75
    weight = last7 / (last7 + VOLUME_HALF)  # 0 -> 1 as volume becomes meaningful
    score = 50 + (raw - 50) * weight
    score += min(SOURCE_BONUS * max(0, n_sources - 1), 12)
    return max(0, min(100, round(score)))


def growth_pct(last7: float, prev7: float) -> float | None:
    """Raw week-over-week % — for display, not ranking."""
    if prev7 <= 0:
        return None
    return (last7 - prev7) / prev7 * 100


def trend_momentum(conn, cluster_ids: list[int]) -> dict[int, dict]:
    """Momentum per trend, aggregated over its entities' signals."""
    if not cluster_ids:
        return {}
    rows = conn.execute(
        """select tce.cluster_id,
                  coalesce(sum(s.value) filter (where s.signal_date >= current_date - 6), 0),
                  coalesce(sum(s.value) filter (where s.signal_date between current_date - 13
                                                and current_date - 7), 0),
                  count(distinct s.source)
           from trend_cluster_entities tce
           left join daily_signals s on s.entity_id = tce.entity_id and s.metric = 'mentions'
           where tce.cluster_id = any(%s)
           group by tce.cluster_id""", (cluster_ids,)).fetchall()
    out = {}
    for cid, last7, prev7, n_sources in rows:
        last7, prev7 = float(last7), float(prev7)
        out[cid] = {
            "momentum": momentum_score(last7, prev7, n_sources or 1),
            "last7": last7, "prev7": prev7,
            "growth_pct": growth_pct(last7, prev7),
            "n_sources": n_sources or 0,
        }
    return out


def label(momentum: int, growth: float | None) -> str:
    """Short human label for a momentum score."""
    if momentum >= 75:
        return "🚀 surging"
    if momentum >= 60:
        return "📈 climbing"
    if momentum >= 45:
        return "➡️ steady"
    return "📉 cooling"
