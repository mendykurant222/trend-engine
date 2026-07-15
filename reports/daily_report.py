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
    rows = conn.execute(
        """select t.id, t.name, t.stage, t.strength, t.confidence, t.first_detected
           from trend_clusters t where t.status = 'active'
           order by t.strength desc, t.last_updated desc limit %s""", (n,),
    ).fetchall()
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
        trends.append({"id": tid, "name": name, "stage": stage, "strength": strength,
                       "confidence": confidence, "first_detected": first_detected,
                       "entities": entities, "companies": companies})
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

    strongest = trends[0]
    summary = (f"{len(trends)} active trends; strongest: {esc(strongest['name'])} "
               f"(strength {strongest['strength']}, {esc(strongest['stage'])}). "
               f"{n_anomalies} anomalies recorded today.")

    lines = [
        f"📊 <b>Trend Engine — Daily Report</b>",
        f"<i>{target_date.strftime('%d.%m.%Y')}</i>",
        "",
        summary,
        "",
        "<b>Top Trends</b>",
    ]
    for i, t in enumerate(trends, 1):
        age = (target_date - t["first_detected"]).days
        lines.append(
            f"{i}. 📈 <b>{esc(t['name'])}</b> — {esc(t['stage'])} | "
            f"strength {t['strength']} | confidence {t['confidence']} | day {age}")
        if t["entities"]:
            lines.append(f"    {esc(', '.join(t['entities'][:6]))}")
        if t["companies"]:
            parts = []
            for ticker, exposure, direction, material in t["companies"]:
                mark = DIRECTION_MARK.get(direction, "?")
                star = "★" if material else ""
                parts.append(f"{esc(ticker)}{mark}{star}")
            lines.append(f"    💼 {' '.join(parts)}")
    lines += ["", "<i>★ = material exposure | + long / − short signal</i>"]
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
    """Extreme cross-source surges only — deliberately high bar (plan item 31)."""
    target_date = target_date or date.today()
    min_score = config.get("alerts", {}).get("min_score", 90)
    rows = conn.execute(
        """select e.canonical_name, a.source, a.kind, a.score, a.details
           from anomalies a join entities e on e.id = a.entity_id
           where a.signal_date = %s and a.score >= %s
             and a.details @> '{"cross_source": true}'::jsonb
           order by a.score desc limit 5""",
        (target_date, min_score),
    ).fetchall()
    alerts = []
    for name, source, kind, score, details in rows:
        alerts.append(
            f"🚨 <b>Trend Alert: {esc(name)}</b>\n"
            f"Cross-source {esc(kind)} — score {score:.0f} ({esc(source)})\n"
            f"today {details.get('today', '?')} vs baseline {details.get('baseline_mean', '?')}")
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
