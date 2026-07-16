"""Dead-man's switch + retention (plan items 57, 64). Runs every 6h from its
own GitHub Actions workflow — independent of the daily pipeline it watches.

- alerts Telegram if no run has been recorded for 30+ hours
- prunes raw payloads older than 120 days (row + hash stay for dedupe)
"""

import sys

from dotenv import load_dotenv

from pipeline import db
from reports.channels import send_telegram

MAX_SILENCE_HOURS = 30
RETENTION_DAYS = 120


def main() -> int:
    load_dotenv()
    conn = db.connect()

    row = conn.execute(
        "select max(started_at), extract(epoch from now() - max(started_at)) / 3600 from runs"
    ).fetchone()
    last, silent_h = row[0], float(row[1] or 0)
    if last is None or silent_h > MAX_SILENCE_HOURS:
        send_telegram(
            "🔴 <b>Trend Engine — dead-man's switch</b>\n"
            f"No pipeline run for {silent_h:.0f}h (last: {last or 'never'}). "
            "Check GitHub Actions.")
        print(f"ALERT sent — silent for {silent_h:.0f}h")
    else:
        print(f"ok — last run {silent_h:.1f}h ago")

    pruned = conn.execute(
        """update raw_items
           set payload = jsonb_build_object('pruned', true, 'type', payload->>'type')
           where collected_at < now() - make_interval(days => %s)
             and not (payload ? 'pruned')""",
        (RETENTION_DAYS,),
    ).rowcount
    if pruned:
        print(f"retention: pruned {pruned} raw payloads older than {RETENTION_DAYS}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
