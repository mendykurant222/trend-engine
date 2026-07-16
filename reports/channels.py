"""Delivery channels shared by all reports: Telegram (HTML) + Resend email."""

import html
import logging
import os

import requests

log = logging.getLogger("channels")


def esc(text: str) -> str:
    """Escape for Telegram/email HTML."""
    return html.escape(str(text), quote=False)


def send_telegram(text: str, html_mode: bool = True) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    payload = {"chat_id": chat, "text": text[:4000],
               "disable_web_page_preview": True}
    if html_mode:
        payload["parse_mode"] = "HTML"
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=30).raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("telegram send failed: %s", exc)
        return False


def send_email(subject: str, html_body: str, config: dict) -> bool:
    key = os.environ.get("RESEND_API_KEY")
    to = config.get("reports", {}).get("email_to")
    if not (key and to):
        return False
    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}"},
            json={"from": os.environ.get("REPORT_FROM", "Trend Engine <onboarding@resend.dev>"),
                  "to": [to], "subject": subject, "html": html_body},
            timeout=30,
        ).raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("email send failed: %s", exc)
        return False
