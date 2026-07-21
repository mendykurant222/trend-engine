"""Daily trend report + extreme alerts (plan items 30, 31, 33).

Distinct from the ops run-summary: this is the PRODUCT message — top trends
with investment insight, Hebrew summary + English detail. Every reported
trend is logged to trend_reports so the weekly report can grade past calls.
"""

import logging
from datetime import date

from reports.channels import esc, send_email, send_telegram

log = logging.getLogger("daily_report")

DIRECTION_MARK = {"positive": "+", "negative": "−"}

# what a human calls each source
SOURCE_NAMES = {
    "reddit": "Reddit", "tiktok": "TikTok", "tiktok_curve": "TikTok",
    "google_trends": "Google searches", "google_trends_interest": "Google searches",
    "amazon": "Amazon best-sellers", "youtube": "YouTube", "gdelt": "News",
    "sec_edgar": "Company filings", "research": "Research reports",
}


def _friendly_sources(sources: list[str]) -> list[str]:
    seen, out = set(), []
    for s in sources:
        name = SOURCE_NAMES.get(s)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _growth_phrase(last7: float, prev7: float, age_days: int) -> str:
    """Plain English, no jargon."""
    if prev7 <= 0:
        return f"brand new — nobody was talking about it a week ago"
    ratio = last7 / prev7
    if ratio >= 10:
        return f"exploded {ratio:.0f}× this week ({prev7:.0f} → {last7:.0f} mentions)"
    if ratio >= 3:
        return f"grew {ratio:.1f}× this week ({prev7:.0f} → {last7:.0f} mentions)"
    if ratio >= 1.25:
        return f"up {(ratio - 1) * 100:.0f}% this week ({prev7:.0f} → {last7:.0f} mentions)"
    return f"steady this week ({last7:.0f} mentions)"


def _why_early(age_days: int, n_sources: int, last7: float, peak_recent: bool) -> str:
    """The proof line: why we think this is still early."""
    bits = []
    if age_days <= 10:
        when = ("first spotted today" if age_days == 0 else
                "first spotted yesterday" if age_days == 1 else
                f"first spotted {age_days} days ago")
        bits.append(when)
    if n_sources >= 3:
        bits.append(f"{n_sources} separate platforms agree")
    elif n_sources == 2:
        bits.append("2 platforms agree")
    else:
        bits.append("one platform only so far — unconfirmed")
    if peak_recent:
        bits.append("still climbing")
    if last7 < 200:
        bits.append("still small — not mainstream yet")
    return " · ".join(bits) if bits else "gaining attention"


def _top_products(conn, n: int) -> list[dict]:
    """The report shows PRODUCTS a person could source and sell — not services,
    games, chains or concepts. Ranked by momentum (rate of change)."""
    from analysis.momentum import momentum_score, growth_pct

    rows = conn.execute(
        """select e.id, e.canonical_name, e.category, e.first_seen, e.product_url,
                  coalesce(sum(s.value) filter (where s.signal_date >= current_date - 6), 0),
                  coalesce(sum(s.value) filter (where s.signal_date between current_date - 13
                                                and current_date - 7), 0),
                  coalesce(array_agg(distinct s.source) filter (where s.source is not null), '{}'),
                  max(s.signal_date) filter (where s.value = (
                      select max(s2.value) from daily_signals s2
                      where s2.entity_id = e.id and s2.metric = 'mentions'
                        and s2.signal_date >= current_date - 29))
           from entities e
           left join daily_signals s on s.entity_id = e.id and s.metric = 'mentions'
           where e.status = 'active' and e.kind = 'product'
           group by e.id
           having coalesce(sum(s.value) filter (where s.signal_date >= current_date - 6), 0) > 0
        """).fetchall()

    products = []
    for eid, name, category, first_seen, url, last7, prev7, sources, peak_date in rows:
        last7, prev7 = float(last7), float(prev7)
        srcs = _friendly_sources(list(sources))
        if not srcs:
            continue
        products.append({
            "id": eid, "name": name, "category": category, "url": url,
            "last7": last7, "prev7": prev7,
            "momentum": momentum_score(last7, prev7, len(srcs)),
            "growth": growth_pct(last7, prev7),
            "sources": srcs, "first_seen": first_seen,
            "peak_recent": bool(peak_date and (date.today() - peak_date).days <= 2),
        })
    products.sort(key=lambda p: (p["momentum"], p["last7"]), reverse=True)
    return products[:n]


def _proof_quote(conn, entity_id: int, name: str) -> str | None:
    """One real thing someone actually posted — the receipt.

    Every quote must be about THIS product. An Amazon best-seller list holds 20
    titles; showing the first one would "prove" a water bottle with a coffee
    listing, so the matching title is located or the quote is dropped.
    """
    words = {w for w in name.lower().split() if len(w) > 2}
    rows = conn.execute(
        """select ri.source, ri.payload from raw_item_entities rie
           join raw_items ri on ri.id = rie.raw_item_id
           where rie.entity_id = %s and ri.source in ('reddit', 'tiktok', 'amazon', 'google_trends')
           order by ri.collected_at desc limit 8""", (entity_id,)).fetchall()
    for source, payload in rows:
        if source == "reddit" and payload.get("title"):
            return f"Reddit: “{payload['title'][:90]}”"
        if source == "tiktok":
            r = payload.get("row", {})
            if r.get("name"):
                return (f"TikTok: #{r['name']} ranked #{r.get('rank', '?')} "
                        f"in {r.get('industry', 'US')}")
        if source == "google_trends" and payload.get("query"):
            val = payload.get("value", "")
            return f"Google: “{payload['query']}” search {val.lower() if val else 'rising'}"
        if source == "amazon" and payload.get("titles"):
            best, best_hits = None, 0
            for t in payload["titles"]:
                title = (t.get("title") or "").lower()
                hits = sum(1 for w in words if w in title)
                if hits > best_hits:
                    best, best_hits = t, hits
            if best and best_hits >= max(1, len(words) // 2):
                return (f"Amazon: #{best.get('rank', '?')} best-seller — "
                        f"{(best.get('title') or '')[:70]}")
    return None


def _top_trends(conn, n: int) -> list[dict]:
    """Ranked by MOMENTUM (rate of change), not strength (size) — ranking by
    size surfaces things at or past their peak, which is the opposite of the
    engine's job."""
    from analysis.momentum import trend_momentum

    candidates = conn.execute(
        """select t.id, t.name, t.stage, t.strength, t.confidence, t.first_detected
           from trend_clusters t where t.status = 'active'
           order by t.last_updated desc limit 60""").fetchall()
    if not candidates:
        return []
    mom = trend_momentum(conn, [r[0] for r in candidates])
    candidates.sort(key=lambda r: (mom.get(r[0], {}).get("momentum", 50), r[3]), reverse=True)
    rows = candidates[:n]

    trends = []
    for tid, name, stage, strength, confidence, first_detected in rows:
        entities = [r[0] for r in conn.execute(
            """select e.canonical_name from trend_cluster_entities tce
               join entities e on e.id = tce.entity_id where tce.cluster_id = %s""",
            (tid,)).fetchall()]
        companies = conn.execute(
            """select c.ticker, tc.exposure, tc.direction, tc.material
               from trend_companies tc join companies c on c.id = tc.company_id
               where tc.cluster_id = %s
               order by tc.material desc, tc.confidence desc limit 6""",
            (tid,)).fetchall()
        m = mom.get(tid, {})
        trends.append({"id": tid, "name": name, "stage": stage, "strength": strength,
                       "confidence": confidence, "first_detected": first_detected,
                       "entities": entities, "companies": companies,
                       "momentum": m.get("momentum", 50),
                       "growth_pct": m.get("growth_pct"),
                       "last7": m.get("last7", 0)})
    return trends


def build_daily_report(conn, config: dict, target_date: date | None = None) -> tuple[str, list[int]]:
    """The product-first daily message: what to look at, why, and where to see it.

    Written for a reader who does not care about the machinery — every line is
    a product, a plain-English reason, and a link. Ranked by how fast talk about
    it is growing, not by how loud it already is.
    """
    target_date = target_date or date.today()
    top_n = config.get("reports", {}).get("daily_top_n", 5)
    products = _top_products(conn, top_n)
    if not products:
        return "", []

    lines = [
        "🛍 <b>Products picking up speed</b>",
        f"<i>{target_date.strftime('%d %B %Y')}</i>",
        "",
    ]
    for i, p in enumerate(products, 1):
        age = (target_date - p["first_seen"]).days
        lines.append(f"<b>{i}. {esc(p['name'].title())}</b>"
                     + (f" <i>({esc(p['category'])})</i>" if p["category"] else ""))
        lines.append(f"📈 {esc(_growth_phrase(p['last7'], p['prev7'], age))}")
        lines.append(f"👀 Seen on: {esc(', '.join(p['sources']))}")
        proof = _proof_quote(conn, p["id"], p["name"])
        if proof:
            lines.append(f"💬 {esc(proof)}")
        lines.append(f"✅ Why it looks early: "
                     f"{esc(_why_early(age, len(p['sources']), p['last7'], p['peak_recent']))}")
        if p["url"]:
            lines.append(f'🛒 <a href="{p["url"]}">See the product</a>')
        lines.append("")

    dash = config.get("reports", {}).get("dashboard_url", "").rstrip("/")
    if dash:
        lines.append(f'<i>Full detail and history: <a href="{dash}">open the dashboard</a></i>')

    # keep grading intact: log the trends these products belong to
    cluster_ids = [r[0] for r in conn.execute(
        """select distinct tce.cluster_id from trend_cluster_entities tce
           join trend_clusters t on t.id = tce.cluster_id and t.status = 'active'
           where tce.entity_id = any(%s)""", ([p["id"] for p in products],)).fetchall()]
    return "\n".join(lines), cluster_ids


def log_reported(conn, cluster_ids: list[int], target_date: date | None = None) -> None:
    target_date = target_date or date.today()
    for cid in cluster_ids:
        conn.execute(
            """insert into trend_reports (cluster_id, reported_date, stage, strength, confidence)
               select id, %s, stage, strength, confidence from trend_clusters where id = %s
               on conflict (cluster_id, reported_date) do update
               set stage = excluded.stage, strength = excluded.strength,
                   confidence = excluded.confidence""",
            (target_date, cid),
        )


def check_alerts(conn, config: dict, target_date: date | None = None) -> list[str]:
    """Extreme cross-source surges (plan item 31), plus a lower bar for
    entities in WATCHED trends (plan item 43)."""
    target_date = target_date or date.today()
    min_score = config.get("alerts", {}).get("min_score", 90)
    watch_min = config.get("alerts", {}).get("watchlist_min_score", 60)
    rows = conn.execute(
        """select e.canonical_name, a.source, a.kind, a.score, a.details,
                  exists (select 1 from trend_cluster_entities tce
                          join trend_clusters t on t.id = tce.cluster_id
                          where tce.entity_id = e.id and t.watched) as watched
           from anomalies a join entities e on e.id = a.entity_id
           where a.signal_date = %s
           order by a.score desc limit 50""",
        (target_date,),
    ).fetchall()
    alerts = []
    for name, source, kind, score, details, watched in rows[:20]:
        cross = details.get("cross_source")
        fires = (cross and score >= min_score) or (watched and score >= watch_min)
        if not fires or len(alerts) >= 5:
            continue
        tag = "⭐ watchlist" if (watched and not (cross and score >= min_score)) else "cross-source"
        if kind == "new_entity":
            detail = (f"{details.get('mentions_in_window', '?')} mentions across "
                      f"{len(details.get('sources') or [])} sources — first seen "
                      f"{details.get('first_seen', 'recently')}")
        elif kind == "acceleration":
            detail = (f"velocity {details.get('velocity_prev', '?')} → "
                      f"{details.get('velocity_now', '?')}/day")
        else:
            detail = f"today {details.get('today', '?')} vs baseline {details.get('baseline_mean', '?')}"
        alerts.append(
            f"🚨 <b>Trend Alert: {esc(name)}</b>\n"
            f"{tag} {esc(kind)} — score {score:.0f} ({esc(source)})\n{esc(detail)}")
    return alerts


def send_daily_report(conn, config: dict, target_date: date | None = None) -> dict:
    """Build, deliver, and log the daily report + any alerts."""
    text, cluster_ids = build_daily_report(conn, config, target_date)
    sent = 0
    if text:
        if send_telegram(text):
            sent += 1
        send_email("Trend Engine — Daily Report",
                   f"<pre>{text}</pre>", config)
        log_reported(conn, cluster_ids, target_date)
    alerts = check_alerts(conn, config, target_date)
    for alert in alerts:
        send_telegram(alert)
    return {"trends_reported": len(cluster_ids), "alerts": len(alerts), "sent": sent}
