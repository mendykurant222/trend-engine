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
    """Returns (telegram-HTML text, reported cluster ids). Empty text = nothing to report."""
    target_date = target_date or date.today()
    top_n = config.get("reports", {}).get("daily_top_n", 5)
    trends = _top_trends(conn, top_n)
    if not trends:
        return "", []

    n_anomalies = conn.execute(
        "select count(*) from anomalies where signal_date = %s", (target_date,)
    ).fetchone()[0]

    from analysis.momentum import label

    top = trends[0]
    summary = (f"Fastest-rising: {esc(top['name'])} "
               f"({label(top['momentum'], top['growth_pct'])}"
               + (f", {top['growth_pct']:+.0f}% week over week" if top["growth_pct"] is not None else "")
               + f"). {n_anomalies} anomalies recorded today.")

    lines = [
        f"📊 <b>Trend Engine — Daily Report</b>",
        f"<i>{target_date.strftime('%d.%m.%Y')}</i>",
        "",
        summary,
        "",
        "<b>Rising fastest</b> <i>(ranked by momentum, not size)</i>",
    ]
    dash = config.get("reports", {}).get("dashboard_url", "").rstrip("/")
    for i, t in enumerate(trends, 1):
        age = (target_date - t["first_detected"]).days
        name = (f'<a href="{dash}/trend/{t["id"]}">{esc(t["name"])}</a>'
                if dash else f"<b>{esc(t['name'])}</b>")
        growth = (f"{t['growth_pct']:+.0f}% wow" if t["growth_pct"] is not None
                  else "new this week")
        lines.append(
            f"{i}. {label(t['momentum'], t['growth_pct'])} {name} — {growth} | "
            f"momentum {t['momentum']} | {esc(t['stage'])} | day {age}")
        if t["entities"]:
            lines.append(f"    {esc(', '.join(t['entities'][:6]))}")
        if t["companies"]:
            parts = []
            for ticker, exposure, direction, material in t["companies"]:
                mark = DIRECTION_MARK.get(direction, "?")
                star = "★" if material else ""
                parts.append(f"{esc(ticker)}{mark}{star}")
            lines.append(f"    💼 market lens: {' '.join(parts)}")
    lines += ["", "<i>💼 = public-market lens (secondary) · ★ material · +/− direction</i>"]
    return "\n".join(lines), [t["id"] for t in trends]


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
