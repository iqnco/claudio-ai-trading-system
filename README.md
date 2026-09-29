# Claudio Inc.

Autonomous swing-trading system for one Schwab cash account ("Luck"), run 24/7 on the host machine.
Rules live in code (`engine/rules.py`), judgment comes from an AI portfolio manager, reports by email.
Read `STRATEGY.md` for the investment policy.

## Pieces
- `engine/run.py`: the trading loop (launchd: `com.example.claudio-engine`, log `logs/engine.log`)
- `bot.py`: Telegram commands and chat (launchd: `com.example.claudioinc-bot`). Never places orders.
- `schwab_api/`: Schwab login, token and account fence (only `SCHWAB_ALLOWED_ACCOUNTS`)
- `data/engine.db`: positions, orders, journal, price cache

## Daily rhythm (US Eastern)
9:00 pre-market news check · 9:15 morning setup · 9:45–15:45 every 5 min: stops, entries, exits ·
15:35 near-close pass · 16:15 nightly scan + AI portfolio manager + email · Saturday weekly review.

## Operator commands (on the host machine, in ~/claudio-inc)
    venv312/bin/python -m engine.admin status
    venv312/bin/python -m engine.admin dryrun      # full cycle, no orders (dry mode only)
    venv312/bin/python -m engine.admin golive      # then: launchctl kickstart -k gui/$(id -u)/com.example.claudio-engine
    venv312/bin/python -m engine.admin halt | resume
    venv312/bin/python -m schwab_api.login --manual   # every 7 days
    venv312/bin/python -m engine.admin aikey       # is the Claude API key working?
    venv312/bin/python -m engine.admin metrics     # scorecard, SPY benchmark, AI attribution
    venv312/bin/python -m engine.backtest          # replay the rules over the bar history
    venv312/bin/python -m unittest tests.test_engine  # rule tests
    venv312/bin/python -m tests.sim_day               # offline end-to-end simulation

## Config (gitignored)
`secrets_local.py`: TELEGRAM_TOKEN, ANTHROPIC_API_KEY, FINNHUB_API_KEY, SCHWAB_APP_KEY/SECRET, RESEND_API_KEY
`config_local.py`: OWNER, TELEGRAM_CHAT_ID, SCHWAB_ALLOWED_ACCOUNTS, SCHWAB_EXCLUDED_SYMBOLS,
SCHWAB_TRADING_ENABLED, REPORT_EMAIL, REPORT_FROM

## Keeping score
Three numbers decide whether this is working, all in the weekly email and in
`engine.admin metrics`:
* **Expectancy** (average R per trade). Positive over 100+ trades is the whole game.
* **Against SPY.** Up 4% means nothing if the index did 6%.
* **AI attribution.** Every candidate the AI is shown goes into a shadow book with its
  decision (taken / vetoed / passed over). Weeks later each one is replayed against the
  bars under the same exit rules, so the trades it rejected get scored too. If the ones
  it bought don't beat the ones it passed on, the AI layer isn't earning its place.

## Claude API key
The engine pings the key daily at 8:30 ET and watches every AI call. If Anthropic refuses the key
(out of credit, revoked, disabled) you get an email right away, then daily until fixed. Rotate by
editing ANTHROPIC_API_KEY in `secrets_local.py`: engine and bot read it fresh, no restart needed;
you get a "working again" email within 30 minutes. While the key is dead, stops and rule-based
exits keep running; new trades pause.
