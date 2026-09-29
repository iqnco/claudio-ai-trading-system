"""Claudio rulebook v1: every number the engine trades by lives here.

STRATEGY.md is the human-readable version of this file. If you change a
number here, change it there too. Nothing else in the engine hard-codes a
trading parameter.
"""

# ── Account ──────────────────────────────────────────────────────────────
# Which Schwab account is decided by SCHWAB_ALLOWED_ACCOUNTS in config_local.py.
# Cash account: settled cash only, no margin, no shorting, no options.

# ── Risk per trade & portfolio limits ────────────────────────────────────
RISK_PER_TRADE = 0.015          # equity lost if the initial stop is hit
MAX_POSITIONS = 10
MAX_POSITION_PCT = 0.20         # of equity, at entry
MAX_POSITION_PCT_SMALLCAP = 0.10
SMALLCAP_MAX_MCAP = 2e9         # below this counts as small cap
MAX_SMALLCAP_TOTAL = 0.30       # all small caps together, of equity
MAX_SECTOR_PCT = 0.35           # one sector, of equity
MAX_OPEN_RISK = 0.075           # "heat": sum of (price - stop) * qty, of equity
                                # 7.5% = ~5 full-size trades at once. Kept below the 10%
                                # drawdown halt on purpose: see DAILY_LOSS_HALT below.
MAX_VALUE_POSITIONS = 4
MIN_POSITION_USD = 300          # below this a trade isn't worth the slippage
MIN_CASH_PCT = 0.0              # cash the engine may never spend (CashMoney keeps a floor)
MAX_NEW_ENTRIES_PER_DAY = 3
MAX_NEW_ENTRIES_PER_MONTH = None   # off for Luck; the CashMoney profile sets it
REENTRY_COOLDOWN_DAYS = 5       # no re-buying a ticker after being stopped out

# ── Kill switches ────────────────────────────────────────────────────────
DAILY_LOSS_HALT = 0.05          # vs start-of-day equity: no new entries today
TOTAL_DRAWDOWN_HALT = 0.10      # vs peak equity: full halt until /resume

# ── Universe ─────────────────────────────────────────────────────────────
MIN_MCAP = 300e6
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5e6         # 50-day average close * volume
MIN_IPO_AGE_DAYS = 90
MIN_HISTORY_BARS = 210          # need a 200-day average
EXCLUDED_NAME_WORDS = ("acquisition corp", "acquisition co", " spac", "warrant",
                       " right", " unit", "preferred", "depositary share", "notes due")
LEVERAGED_ETF_HINTS = ("2x", "3x", "ultra", "leveraged", "inverse", "bear ", "bull ")

# ── Market regime (SPY) ──────────────────────────────────────────────────
# full: SPY > 50d and 200d MA.  caution: SPY < 50d  -> risk x0.5
# risk_off: SPY < 200d or VIX > 30 -> no momentum / catalyst entries, bounce x0.5
VIX_RISK_OFF = 30.0
CAUTION_RISK_MULT = 0.5

# ── Setups ───────────────────────────────────────────────────────────────
MOMENTUM = dict(
    lookback_high=20, vol_mult=1.5, max_stop_pct=0.10, stop_atr=1.5,
    max_extension=0.03,          # don't chase more than 3% above the trigger
    trail_low_days=10, max_hold_days=15, time_stop_days=7,
)
BOUNCE = dict(
    rsi2_max=10.0, drop_from_10d_high=0.08, stop_atr=1.5, max_stop_pct=0.10,
    exit_ma=5, target_r=2.0, max_hold_days=5,
)
CATALYST = dict(
    min_gap=0.05, vol_mult=3.0, max_stop_pct=0.10, trail_low_days=10,
    max_hold_days=15, time_stop_days=7, earnings_window_days=3,
)
VALUE = dict(
    max_stop_pct=0.12, stop_atr=2.5, trail_ma=50, trail_ma_buffer=0.01,
    max_hold_days=63, reclaim_days=5, max_pe_vs_sector=0.8,
    min_rev_growth=5.0, max_debt_to_equity=150.0,
)
# Holdings bought before Claudio took over ("legacy"). Keepers get a trend stop and are
# trimmed to fit a shared risk budget; the rest are sold in 3 daily tranches.
LEGACY = dict(stop_atr=2.5, max_stop_pct=0.15, trail_ma=50, exit_tranches=3,
              heat_budget=0.035,         # keepers share this; leaves ~1% for the first new trade
              ma_buffer=0.02,            # trend stop sits 2% under the 50-day (or 200-day) average
              min_stop_pct=0.03,         # never closer than 3% to the price
              tranche_time_et=(10, 30),
              max_hold_days=20)          # then sold, unless it's up >= 1R (then it trails)
# US wash-sale rule: a loss is deferred if the same stock is bought back within 30 days.
# Holdings sold at a loss on day 1 are blocked from re-entry for 31 days.
NO_REBUY_AFTER_LOSS_DAYS = 31

# ── Trade management (all setups) ────────────────────────────────────────
BREAKEVEN_AT_R = 1.0            # stop to entry at +1R, measured on the CLOSE (see risk.py)
PARTIAL_AT_R = 3.0              # sell a third at +3R, once the trade has proved itself
PARTIAL_FRACTION = 1 / 3
EXIT_BEFORE_EARNINGS = ("momentum", "bounce")   # flat the day before the report
HOLD_THROUGH_EARNINGS = False   # True disables every earnings exit (CashMoney profile)

# ── Runners ──────────────────────────────────────────────────────────────
# A trade that reaches +3R on a closing basis stops being a swing trade. The whole
# profit of a momentum book comes from the few positions that run for months, so a
# runner loses its time limits and trails slowly enough to sit through a pullback.
RUNNER_AT_R = 3.0               # measured on high_water (the best close since entry)
RUNNER_TRAIL_LOW_DAYS = 20      # trail the 20-day low instead of the 10-day
RUNNER_TRAIL_MA = 50            # ...and never below the 50-day average once above it
# Earnings with a cushion: sell half, run the rest through the report.
EARNINGS_HOLD_SETUPS = ("momentum", "catalyst")
EARNINGS_HOLD_MIN_R = 2.0       # ...only this far in profit, with the stop at or above entry
EARNINGS_HOLD_FRACTION = 0.5
VALUE_EARNINGS_MAX_PCT = 0.08   # value may hold through earnings only if <= 8% and >= +1R

# ── Execution ────────────────────────────────────────────────────────────
TRADE_START_ET = (9, 45)
TRADE_END_ET = (15, 45)
BOUNCE_CHECK_ET = (15, 35)      # bounce entries/exits evaluated near the close
ENTRY_LIMIT_SLIPPAGE = 0.003    # limit = ask * (1 + this)
MAX_QUOTE_DEVIATION = 0.01      # reject if limit is > 1% from last price
ENTRY_ORDER_TTL_MIN = 20
MAX_ENTRY_ATTEMPTS = 2          # re-arm a watch after an unfilled entry at most this often
MIN_STOP_RAISE = 0.005          # only replace a stop if it moves up >= 0.5%
# Exits (targets, time stops, pre-earnings, legacy tranches) use market orders during
# the regular session so they always fill. Entries are always limit orders.
LOOP_SECONDS = 300

# ── Setups on/off (the weekly strategist can propose switching one off) ─
SETUPS_ENABLED = dict(momentum=True, bounce=True, catalyst=True, value=True)
# Per-setup risk multiplier: a setup on probation trades at reduced size instead of
# being switched off, so it keeps producing evidence. Raised or lowered only with a
# trade record behind it (see CHANGELOG).
SETUP_SIZE_MULT = dict(momentum=0.5, bounce=1.0, catalyst=1.0, value=1.0)

# ── AI portfolio manager ─────────────────────────────────────────────────
PM_MAX_CANDIDATES = 12          # best scanner hits shown to the nightly PM call
PM_MAX_PER_SETUP = 4
PM_MAX_TOKENS = 3000
PM_MEMO_HISTORY = 3             # previous memos fed back for continuity
PM_MIN_TIGHTEN_GAP = 0.01       # a tightened stop must stay >= 1% below the price
PREMARKET_MINUTES_BEFORE_OPEN = 35
ANALYST_MAX_TOKENS = 700

# ── Index core (CashMoney only; Luck has no core position) ───────────────
CORE = None
TREND_BREAK_MA = None           # monthly close below this average sells the position

# ── Setup health ─────────────────────────────────────────────────────────
SETUP_REVIEW_MIN_TRADES = 20    # flag a setup whose expectancy < 0 after this many

# ── Profiles ─────────────────────────────────────────────────────────────
# A second account (CashMoney) runs the same engine with a different rulebook.
# CLAUDIO_PROFILE=cash makes engine/profiles/cash.py overwrite the constants above;
# unset (the Luck engine) nothing below this line does anything at all.
import os as _os

PROFILE = (_os.environ.get("CLAUDIO_PROFILE") or "luck").strip().lower()
if PROFILE not in ("", "luck"):
    from importlib import import_module as _imp
    _overlay = _imp(f"{__package__}.profiles.{PROFILE}")
    for _k in dir(_overlay):
        if _k.isupper() and not _k.startswith("_"):
            globals()[_k] = getattr(_overlay, _k)
    del _imp, _overlay, _k
