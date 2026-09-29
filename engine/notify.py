"""Alerts. Claudio runs on its own; it only interrupts you when something needs you.

config_local.ALERTS:
  "email"         (default) critical alerts by email, nothing on Telegram
  "telegram"      critical alerts on Telegram
  "telegram_all"  every fill, sell and stop move on Telegram too
Routine activity (buys, sells, stop moves) is always in the daily email and the log.
Critical = kill switch / daily loss halt, Schwab login expiring, a stop that failed or went
missing, engine errors, the AI portfolio manager failing, pre-market sells, manual halt/resume.
"""
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_last = {}


def redact(text):
    try:
        from secrets_local import TELEGRAM_TOKEN
        if TELEGRAM_TOKEN:
            text = str(text).replace(TELEGRAM_TOKEN, "<telegram-token>")
    except Exception:
        pass
    return text


def _mode():
    try:
        import config_local
        return getattr(config_local, "ALERTS", "email")
    except Exception:
        return "email"


def telegram(text):
    """Raw Telegram send (also the last-resort fallback when email fails)."""
    try:
        from secrets_local import TELEGRAM_TOKEN
        from config_local import TELEGRAM_CHAT_ID
        for i in range(0, len(text), 4000):
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i:i + 4000]}, timeout=15)
        return True
    except Exception as e:
        print("telegram send failed:", redact(e), flush=True)
        return False


def send(text, key=None, every=0, critical=False):
    """Log always; deliver only what the ALERTS mode says. key+every rate-limits repeats."""
    print(f"[alert{'!' if critical else ''}] {text}", flush=True)
    mode = _mode()
    if not critical and mode != "telegram_all":
        return
    if key and every and time.time() - _last.get(key, 0) < every:
        return
    if key:
        _last[key] = time.time()
    if mode == "email" and critical:
        from . import mailer
        first = text.strip().splitlines()[0][:90] if text.strip() else "alert"
        body = "<p>" + mailer.esc(text).replace("\n", "<br>") + "</p>"
        mailer.send(f"Claudio alert: {first}", body, text)
        return
    telegram(text)
