"""Schwab settings, following claudio-inc conventions.

Secrets  -> secrets_local.py (gitignored): SCHWAB_APP_KEY, SCHWAB_APP_SECRET
Personal -> config_local.py  (gitignored): SCHWAB_TRADING_ENABLED (default False)
"""
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _get(module: str, name: str, default=None):
    try:
        return getattr(importlib.import_module(module), name, default)
    except ImportError:
        return default


def _req(name: str) -> str:
    val = (_get("secrets_local", name) or "").strip()
    if not val:
        raise SystemExit(f"Missing {name} in secrets_local.py (see secrets_local.example.py).")
    return val


APP_KEY = lambda: _req("SCHWAB_APP_KEY")
APP_SECRET = lambda: _req("SCHWAB_APP_SECRET")
CALLBACK_URL = "https://127.0.0.1:8182"   # must match the Claudio app on developer.schwab.com
TOKEN_PATH = os.environ.get("CLAUDIO_TOKEN") or str(ROOT / "schwab_api" / "token.json")

# A profile (CashMoney) must never inherit Luck's account by accident, so when
# CLAUDIO_PROFILE is set the account list comes from the environment ONLY. An empty
# list means every Schwab call fails closed, which is the right way to fail.
PROFILE = (os.environ.get("CLAUDIO_PROFILE") or "luck").strip().lower()
_profiled = PROFILE not in ("", "luck")


def _csv(name):
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


# Last 4 digits of the only accounts Claudio may touch. Anything else is ignored.
ALLOWED_ACCOUNTS = {str(x) for x in (
    _csv("CLAUDIO_ACCOUNTS") if _profiled
    else (_get("config_local", "SCHWAB_ALLOWED_ACCOUNTS", []) or []))}
# Holdings Claudio must never touch (the owner's own picks). Excluded from equity too.
EXCLUDED_SYMBOLS = {str(x).upper() for x in (
    _csv("CLAUDIO_EXCLUDED") if _profiled
    else (_get("config_local", "SCHWAB_EXCLUDED_SYMBOLS", []) or []))}
TRADING_ENABLED = ((os.environ.get("CLAUDIO_TRADING") == "1") if _profiled
                   else _get("config_local", "SCHWAB_TRADING_ENABLED", False) is True)

# Schwab refresh tokens die after 7 days. Warn a day early.
TOKEN_MAX_AGE_S = 7 * 24 * 3600
TOKEN_WARN_AGE_S = 6 * 24 * 3600
