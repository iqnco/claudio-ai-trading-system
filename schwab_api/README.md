# schwab_api — Schwab Trader API for Claudio

App on developer.schwab.com: **Claudio** (Accounts & Trading + Market Data, Production).
Callback: `https://127.0.0.1:8182`. Runs on the host machine under `venv312` (Python 3.12; schwab-py needs 3.10+).

## Setup (once)
1. In `secrets_local.py` add `SCHWAB_APP_KEY = "..."` and `SCHWAB_APP_SECRET = "..."`
   (developer.schwab.com → Dashboard → Claudio → reveal Client ID / Client Secret).
2. `venv312/bin/python -m schwab_api.login --manual` → open the printed URL on any device,
   log in, approve, tick both accounts, then paste the final 127.0.0.1 URL back.
3. `venv312/bin/python -m schwab_api.test_connection` → accounts, positions, SPY quote.

## Every 7 days
Schwab kills the refresh token after 7 days: re-run step 2.
`token_status()` returns `warn=True` after day 6. Hook it into the Telegram bot.

## Trading
Off unless `SCHWAB_TRADING_ENABLED = True` in `config_local.py`.
Every order function must call `assert_trading_enabled()` first.
