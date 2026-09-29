"""Shared Schwab client for the rest of Claudio.

    from schwab_api.client import get_client, token_status
"""
import json
import time
from pathlib import Path
from schwab import auth
from . import config


class TokenExpired(RuntimeError):
    pass


def token_status() -> dict:
    """Age of the refresh token. Use this to ping Telegram before it dies."""
    p = Path(config.TOKEN_PATH)
    if not p.exists():
        return {"exists": False, "needs_login": True}
    created = json.loads(p.read_text()).get("creation_timestamp", p.stat().st_mtime)
    age = time.time() - created
    return {
        "exists": True,
        "age_hours": round(age / 3600, 1),
        "hours_left": round((config.TOKEN_MAX_AGE_S - age) / 3600, 1),
        "warn": age > config.TOKEN_WARN_AGE_S,
        "needs_login": age > config.TOKEN_MAX_AGE_S,
    }


def get_client():
    st = token_status()
    if st["needs_login"]:
        raise TokenExpired("Schwab token missing or expired. Run: venv312/bin/python -m schwab_api.login --manual")
    return auth.client_from_token_file(config.TOKEN_PATH, config.APP_KEY(), config.APP_SECRET())


def account_hashes(client) -> dict:
    """{last-4: hashValue} for ALLOWED accounts only (Luck); CashMoney is never returned. The API wants the hash, not the number."""
    r = client.get_account_numbers()
    r.raise_for_status()
    if not config.ALLOWED_ACCOUNTS:
        raise PermissionError("Set SCHWAB_ALLOWED_ACCOUNTS in config_local.py before using Schwab.")
    return {a["accountNumber"][-4:]: a["hashValue"] for a in r.json()
            if a["accountNumber"][-4:] in config.ALLOWED_ACCOUNTS}


def assert_trading_enabled():
    """Call this at the top of any function that places, replaces or cancels orders."""
    if not config.TRADING_ENABLED:
        raise PermissionError("Trading is disabled (set SCHWAB_TRADING_ENABLED = True in config_local.py).")
