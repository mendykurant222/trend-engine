"""Daily run summary (plan item 8): what succeeded/failed, how much was
collected, and month-to-date spend vs budget.

Delivery: Resend email if RESEND_API_KEY is set; Telegram if bot token is set;
always logged to stdout (which GitHub Actions captures).
"""

import logging
import os

import requests

log = logging.getLogger("summary")


def build_text(run_id: int, status: str, results: list, spend: list) -> str:
    lines = [f"Trend Engine run #{run_id} — {status.upper()}", ""]
    for name, st, seen, stored, error in results:
        mark = {"ok": "✅", "failed": "❌", "skipped": "⏭️"}.get(st, "?")
        line = f"{mark} {name}: {st}"
        if st == "ok":
            line += f" — {seen} items seen, {stored} new"
        elif error:
            line += f" — {error[:200]}"
        lines.append(line)
    lines.append("")
    total = sum(c for _, c in spend)
    lines.append(f"Month-to-date spend: ${total:.2f}")
    for provider, cost in spend:
        lines.append(f"  {provider}: ${cost:.2f}")
    return "\n".join(lines)


def send_summary(config: dict, run_id: int, status: str, results: list, spend: list) -> None:
    text = build_text(run_id, status, results, spend)
    print("\n" + text + "\n")

    budget = config.get("budget", {})
    total = sum(c for _, c in spend)
    cap = budget.get("monthly_total_usd", 0)
    if cap and total > cap * budget.get("alert_at_pct", 80) / 100:
        text = f"⚠️ BUDGET: ${total:.2f} of ${cap} monthly cap\n\n" + text

    resend_key = os.environ.get("RESEND_API_KEY")
    email_to = config.get("reports", {}).get("email_to")
    if resend_key and email_to:
        try:
            requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}"},
                json={
                    "from": os.environ.get("REPORT_FROM", "Trend Engine <onboarding@resend.dev>"),
                    "to": [email_to],
                    "subject": f"Trend Engine run #{run_id}: {status}",
                    "text": text,
                },
                timeout=30,
            ).raise_for_status()
            log.info("summary emailed to %s", email_to)
        except requests.RequestException as exc:
            log.error("email failed: %s", exc)

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": text},
                timeout=30,
            ).raise_for_status()
            log.info("summary sent to Telegram")
        except requests.RequestException as exc:
            log.error("telegram failed: %s", exc)
