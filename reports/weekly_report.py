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

    # prediction performance + automated precision metric — plan items 32, 47
    lines += ["", "<b>Prediction performance</b>"]
    any_perf = False
    for days_back in (14, 30):
        rows = _performance_rows(conn, target_date, days_back)
        if not rows:
            continue
        any_perf = True
        hits = 0
        lines.append(f"  <i>reported {days_back}d ago:</i>")
        for name, s_then, stage_then, s_now, stage_now, status in rows:
            # a call "held up" if the trend is still active and kept >=70% of
            # its reported strength — the continuous precision definition
            held = status == "active" and s_now >= s_then * 0.7
            hits += held
            if status != "active":
                verdict = "❌ dead"
            elif s_now >= s_then:
                verdict = f"✅ {s_then}→{s_now}"
            else:
                verdict = f"{'⚠️' if held else '❌'} {s_then}→{s_now}"
            lines.append(f"    {esc(name)}: {verdict} ({esc(stage_then)}→{esc(stage_now)})")
        lines.append(f"  <b>Precision ({days_back}d): {100 * hits // len(rows)}%</b> ({hits}/{len(rows)})")
    if not any_perf:
        lines.append("  (no trends old enough to grade yet)")

    # engine quality digest (plan item 29's weekly review, automated)
    llm_rows = conn.execute(
        """select purpose, count(*), coalesce(sum(cost_usd), 0)
           from llm_calls where created_at >= %s
           group by purpose order by 3 desc""", (week_ago,)).fetchall()
    new_entities = conn.execute(
        "select count(*) from entities where first_seen >= %s", (week_ago,)).fetchone()[0]
    merges = conn.execute(
        """select count(*) from entity_aliases
           where created_at >= %s and (source like 'trgm%%' or source like 'haiku%%')""",
        (week_ago,)).fetchone()[0]
    lines += ["", "<b>Engine quality (7d)</b>",
              f"  🏷️ {new_entities} new entities · {merges} fuzzy alias merges"]
    for purpose, count, cost in llm_rows:
        lines.append(f"  🤖 {esc(purpose)}: {count} calls, ${float(cost):.2f}")

    lines += ["", f"💰 Month-to-date spend: ${float(spend):.2f}"]
    return "\n".join(lines)


def send_weekly_report(conn, config: dict, target_date: date | None = None) -> bool:
    text = build_weekly_report(conn, config, target_date)
    ok = send_telegram(text)
    send_email("Trend Engine — Weekly Report", f"<pre>{text}</pre>", config)
    return ok
