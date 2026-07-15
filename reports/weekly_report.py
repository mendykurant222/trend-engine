"""Weekly summary + prediction performance (plan item 32).

The performance table is the minimal feedback loop, live from week one: every
trend reported ~14 and ~30 days ago is compared with its state today —
did the call hold up? The fully automated precision metric (plan item 47)
extends this in Phase 7.
"""

import logging
from datetime import date, timedelta

from reports.channels import esc, send_email, send_telegram

log = logging.getLogger("weekly_report")


def _performance_rows(conn, target_date: date, days_back: int, tolerance: int = 3):
    return conn.execute(
        """select distinct on (t.id)
                  t.name, r.strength, r.stage, t.strength, t.stage, t.status
           from trend_reports r join trend_clusters t on t.id = r.cluster_id
           where r.reported_date between %s and %s
           order by t.id, r.reported_date""",
        (target_date - timedelta(days=days_back + tolerance),
         target_date - timedelta(days=days_back - tolerance)),
    ).fetchall()


def build_weekly_report(conn, config: dict, target_date: date | None = None) -> str:
    target_date = target_date or date.today()
    week_ago = target_date - timedelta(days=7)

    new_trends = conn.execute(
        """select name, stage, strength from trend_clusters
           where first_detected >= %s order by strength desc""", (week_ago,),
    ).fetchall()
    active = conn.execute(
        "select count(*) from trend_clusters where status = 'active'").fetchone()[0]
    died = conn.execute(
        """select name from trend_clusters
           where status = 'dead' and last_updated >= %s""", (week_ago,),
    ).fetchall()
    week_anomalies = conn.execute(
        "select count(*) from anomalies where signal_date >= %s", (week_ago,),
    ).fetchone()[0]
    spend = conn.execute(
        """select coalesce(sum(cost_usd), 0) from api_costs
           where created_at >= date_trunc('month', now())""").fetchone()[0]

    summary = (f"Week in review: {len(new_trends)} new trends, {active} active in total, "
               f"{week_anomalies} anomalies this week.")

    lines = [
        "🗓 <b>Trend Engine — Weekly Report</b>",
        f"<i>{week_ago.strftime('%d.%m')} – {target_date.strftime('%d.%m.%Y')}</i>",
        "",
        summary,
        "",
        f"<b>New trends this week ({len(new_trends)})</b>",
    ]
    if new_trends:
        for name, stage, strength in new_trends[:8]:
            lines.append(f"  ✨ {esc(name)} — {esc(stage)}, strength {strength}")
    else:
        lines.append("  (none)")
    if died:
        lines.append(f"\n<b>Moved to graveyard</b>: {esc(', '.join(r[0] for r in died))}")

    # prediction performance — plan item 32
    lines += ["", "<b>Prediction performance</b>"]
    any_perf = False
    for days_back in (14, 30):
        rows = _performance_rows(conn, target_date, days_back)
        if not rows:
            continue
        any_perf = True
        lines.append(f"  <i>reported {days_back}d ago:</i>")
        for name, s_then, stage_then, s_now, stage_now, status in rows:
            if status != "active":
                verdict = "❌ dead"
            elif s_now >= s_then:
                verdict = f"✅ {s_then}→{s_now}"
            else:
                verdict = f"⚠️ {s_then}→{s_now}"
            lines.append(f"    {esc(name)}: {verdict} ({esc(stage_then)}→{esc(stage_now)})")
    if not any_perf:
        lines.append("  (no trends old enough to grade yet)")

    lines += ["", f"💰 Month-to-date spend: ${float(spend):.2f}"]
    return "\n".join(lines)


def send_weekly_report(conn, config: dict, target_date: date | None = None) -> bool:
    text = build_weekly_report(conn, config, target_date)
    ok = send_telegram(text)
    send_email("Trend Engine — Weekly Report", f"<pre>{text}</pre>", config)
    return ok
