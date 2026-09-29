# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`): patch for
fixes, minor for backwards-compatible additions, major for breaking changes
(e.g. a `config_local.py` key being renamed or removed).

## [2.3.0] - 2026-08-05

- **Fixed:** free-form chat claimed it had no live market access. The
  `analyze`/`quick`/`technical`/`health`/`macro` commands always had data,
  but `run_claude()` called the model with no tools, so anything asked
  outside a command ("what's NVDA at?") was answered from training data or
  refused. Chat now has three tools — `get_quote`, `get_market_snapshot`,
  `get_news` — and the system prompt tells it to look prices up rather than
  recall them.
- Added `market_data.live_quote()` (Finnhub `/quote` real-time, falling back
  to yfinance ~15-min delayed) and `market_data.market_snapshot()`
  (SPY/QQQ/VIX/10Y). Each returns the source it used, so the bot can say
  when a number is delayed instead of implying it's live.

## [2.2.0] - 2026-07-02

- Removed `PAPER_TRADING`/"PAPER TRADE" mode labels and `INITIAL_CAPITAL`.
  Position sizing is now always a % of total portfolio (half-Kelly capped
  at `MAX_POSITION_SIZE_PCT`), not a dollar amount or share count tied to
  a fixed hypothetical account — works the same at any portfolio size.
  Entry strategy (price levels, stop/targets, R/R) is unchanged; only how
  position size and max-loss are expressed changed, from $ to %.

## [2.1.0] - 2026-07-02

Reliability, cost, and chat-memory fixes from real usage feedback.

- **Fixed:** `analyze TICKER` sent every brief to Telegram twice. `cio_agent.py`
  was sending it directly (`send_telegram()`) *and* `bot.py` was separately
  relaying the same output — only one send now happens, from `bot.py`.
- **Fixed:** the bot was fully synchronous — a multi-minute analysis blocked
  the poll loop, so it looked unresponsive to anything sent in that window.
  Message handling now runs in a background thread per message.
- **Fixed:** free-form chat had no real memory — it shelled out to the
  `claude` CLI with the whole history flattened into one text blob per call.
  It now calls the Anthropic API directly with a proper multi-turn
  `messages` array, so the model actually sees prior turns. Also removes the
  Claude Code CLI as a runtime dependency for chat.
- **Cost/performance:** a full analysis used to fetch the same ticker's
  yfinance data independently in 6 places (once per agent, plus once more
  in the CIO synthesis step), and SPY history separately in both the macro
  and risk agents. All of that is now fetched once (`agents/market_data.py`)
  and shared. Also cut a redundant separate 3-month yfinance fetch in the
  macro agent by slicing the already-fetched 1-year history instead.
- **Cost:** health/technical/risk agents (which mostly narrate numbers
  Python already computed, not open-ended judgment) now run on
  `claude-haiku-4-5-20251001` instead of Sonnet. Fundamental, macro, and the
  CIO synthesis — the calls that need real judgment — stay on Sonnet.
  Configurable via `MODEL_MAIN`/`MODEL_FAST` in `config/settings.py`.
- **Leaner:** removed `FMP_API_KEY` — it was collected by the setup wizard
  but never actually used anywhere in the code. One less key to configure.
  (Non-breaking: an old `secrets_local.py` with a leftover `FMP_API_KEY`
  line still works fine, it's just unused.)

## [2.0.0] - 2026-07-01

**Breaking:** merged in the [claude-telegram-bot](https://github.com/iqnco/claude-telegram-bot)
repo — `bot.py` now lives here instead of a separate sibling repo. That
repo is archived; this is now the single source of truth for both the
analysis agents and the Telegram bot.

- `bot.py` moved into this repo (was `claude-telegram-bot/bot.py`); no more
  `CLAUDIO_INC_PATH` sibling-repo indirection — paths resolve within this repo
- Unified config: `TELEGRAM_TOKEN` (already in `secrets_local.py`) and
  `TELEGRAM_CHAT_ID` (already in `config_local.py`) now also drive the bot
  directly — `TELEGRAM_CHAT_ID` doubles as the bot's allowlist ID, replacing
  the separate `TELEGRAM_ALLOWED_ID` the old bot repo used
- `setup.py` is now one combined wizard covering both agents and bot,
  including the Claude Code CLI check and optional launchd auto-start that
  used to live in the separate bot repo's wizard
- If you have an old `claude-telegram-bot` checkout, it still runs, but
  won't receive further updates — migrate by following this repo's Quick Start

## [1.0.0] - 2026-07-01

Initial public release.

- `setup.py` interactive wizard: Anthropic + market-data API keys, optional
  Telegram integration, venv, database init
- Owner name and Telegram chat ID moved out of the previously-committed
  `config/settings.py` into a gitignored `config_local.py`
- All agent/database paths resolve relative to their own file location
  instead of a hardcoded `~/claudio-inc`
- Genericized the CIO agent's persona text to use the configured owner name
rules (no historical earnings dates), the AI layer, and delisted names.

## 2026-09-24 - CashMoney: a second, more careful account

Built the CashMoney strategy as a second profile of the same engine rather than a second
copy of it. `CLAUDIO_PROFILE=cash` overlays `engine/profiles/cash.py` onto `rules.py`;
with the variable unset the Luck engine is byte-for-byte unchanged, which the test suite
now checks in both directions (95 tests, run in both orders).

What it is: a 45% index core held on a monthly 200-day trend rule with a 2% re-entry
buffer, plus up to 8 value satellites at 1% risk each, 4% total heat, a 2% cash floor,
and a -8% drawdown halt. One setup only. Wide stops (2.5 ATR, capped at 18%) trailing 2%
under the 200-day. No time stops, no partials, earnings held through.

The screen adds the rule Luck's value setup does not have: price above a 200-day average
that is itself flat or rising, and not more than 30% extended above it. Cheap below a
falling average is where value investing goes to die.

Changes to shared code, all default-preserving: a per-profile trailing average
(`VALUE["trail_ma"]`), `HOLD_THROUGH_EARNINGS`, `MAX_NEW_ENTRIES_PER_MONTH`, `MIN_CASH_PCT`,
a `core` holding type that is excluded from heat and from the position count, `ma200` in the
bar context, and account/trading/token settings that a profile may only take from its own
environment.

Not done, on purpose: the account is still fenced out. `engine.cashrun` produces the memo
and nothing else. Three explicit steps turn it on; see STRATEGY_CASH.md.

What cannot be promised: the value screen has never been backtested, because the
fundamentals available here are today's numbers, not what was known at the time. The trend
filter, the stops and the core timing rule can be and have been tested.
