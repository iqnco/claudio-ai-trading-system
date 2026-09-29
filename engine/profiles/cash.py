"""CashMoney rulebook: the careful account.

STRATEGY_CASH.md is the human-readable version of this file. Every name here
overwrites the same name in rules.py when CLAUDIO_PROFILE=cash; anything not
named here keeps the Luck value, so read this next to rules.py, not instead of it.

The shape: an index core held on a monthly trend rule, up to eight value
satellites at 1% risk each, and cash that is allowed to just sit there.
"""

# ── Risk per trade & portfolio limits ────────────────────────────────────
# Every percentage here is of the SLEEVE the engine manages, not of the account.
# The index core and the Treasury reserve are on the exclusion list, so the engine's
# equity is its own book (about a third of the account). A 2% risk here is 0.6% of the
# account; the 8% halt is 2.6% of it. See STRATEGY_CASH.md for the arithmetic.
RISK_PER_TRADE = 0.02
MAX_POSITIONS = 8
MAX_POSITION_PCT = 0.15
MAX_POSITION_PCT_SMALLCAP = 0.15    # moot: MIN_MCAP is above the small-cap line
MAX_SMALLCAP_TOTAL = 0.0
MAX_SECTOR_PCT = 0.25       # tighter than Luck: this screen clusters by sector
MAX_OPEN_RISK = 0.08            # new trades only; inherited holdings have their own budget
MAX_VALUE_POSITIONS = 8         # value is the only setup the engine picks
MIN_POSITION_USD = 800
# No cash floor: the Treasury reserve sits outside the engine's book entirely, so every
# dollar the engine can see is dry powder it is meant to be able to deploy.
MIN_CASH_PCT = 0.0
MAX_NEW_ENTRIES_PER_DAY = 1
MAX_NEW_ENTRIES_PER_MONTH = 4   # haste is the enemy in this account
REENTRY_COOLDOWN_DAYS = 21

# ── Kill switches ────────────────────────────────────────────────────────
DAILY_LOSS_HALT = 0.04
TOTAL_DRAWDOWN_HALT = 0.08

# ── Universe: bigger, older, more liquid than Luck's ─────────────────────
MIN_MCAP = 2e9
MIN_PRICE = 10.0
MIN_DOLLAR_VOLUME = 20e6
MIN_IPO_AGE_DAYS = 1095         # three years of being a public company

# ── The only setup ───────────────────────────────────────────────────────
SETUPS_ENABLED = dict(momentum=False, bounce=False, catalyst=False, value=True)
SETUP_SIZE_MULT = dict(momentum=0.0, bounce=0.0, catalyst=0.0, value=1.0)

# mode="trend" switches scan_value to the CashMoney screen: above a flat-or-rising
# 200-day average instead of a fresh 50-day reclaim. Wide stops, no time limit.
VALUE = dict(
    mode="trend",
    stop_atr=2.5, max_stop_pct=0.18,
    trail_ma=200, trail_ma_buffer=0.02,     # the stop trails 2% under the 200-day
    max_hold_days=None,                     # no time stop: nothing here is in a hurry
    max_pe_vs_sector=0.8,
    # ...but not absurdly cheap. A P/E a third of its sector's is not a bargain the market
    # overlooked, it is the market pricing in an earnings collapse, and it is usually right.
    min_pe_vs_sector=0.35,
    min_rev_growth=0.0,                     # not shrinking
    max_debt_to_equity=150.0,               # a percentage: the universe median is 41
    min_current_ratio=1.0,                  # checked only when the figure exists (banks have none)
    min_roe=8.0,                            # a business that cannot earn on its capital is not value

    # The peak-cycle guards. A miner after the metal has run, a tanker after freight rates
    # have spiked: trailing earnings are at a high, so the P/E looks tiny, and then earnings
    # normalise and the cheapness was an illusion. These two catch it.
    max_eps_growth=120.0,                   # EPS more than doubling in a year is a cycle, not a trend
    min_margin_mrq_vs_ttm=0.75,             # latest quarter's margin vs the trailing year's:
                                            # well below means the peak has already rolled over

    ma200_slope_days=21,                    # "flat or rising" measured over a month
    max_extension=0.20,                     # 20%+ above the 200-day is momentum wearing a value label
    reclaim_days=5,                         # unused in trend mode, kept for shape
)

# ── Trade management ─────────────────────────────────────────────────────
BREAKEVEN_AT_R = 1.0
PARTIAL_AT_R = 1e9              # no partials: the trailing stop is the profit taker
RUNNER_AT_R = 1e9               # runner mode is a momentum idea; nothing here needs it
EXIT_BEFORE_EARNINGS = ()
EARNINGS_HOLD_SETUPS = ()
HOLD_THROUGH_EARNINGS = True    # positions are small and the horizon is long

# ── Holdings the owner brought with him ─────────────────────────
# His four stocks and ETHA are managed like anything else: a stop from day one, the
# stop only moves up, a broken trend sells the position. They get a heat budget of their
# own so an inherited book cannot eat the whole allowance and leave nothing to trade with.
LEGACY = dict(stop_atr=2.5, max_stop_pct=0.15, trail_ma=50, exit_tranches=3,
              heat_budget=0.08,
              ma_buffer=0.02,
              min_stop_pct=0.03,
              tranche_time_et=(10, 30),
              max_hold_days=60)      # longer than Luck's 20: this account is patient

# ── The index core ───────────────────────────────────────────────────────
# Held while the index is in an uptrend, checked once a month on purpose. This rule is
# famous for being ruined by people who check it daily.
# The owner holds the index core himself, permanently, with no timing rule (see
# STRATEGY_CASH.md, "Protection"). The engine has no core and no business buying one:
# every core fund is on its exclusion list. cashcore.core_plan stays in the codebase,
# tested and dormant, in case that decision is ever revisited.
CORE = dict(
    symbol="SCHX",
    trend_ma=200,          # the line the monthly close is judged against
    reentry_buffer=0.02,   # free to leave, has to clear the line by 2% to come back
    target_pct=0.50,       # of total account equity
    rebalance_band=0.10,   # leave it alone unless it is >10% off target
    min_trade_usd=500,
)
# A satellite whose month ends below its 200-day average is sold, stop or no stop.
TREND_BREAK_MA = 200

# ── AI portfolio manager ─────────────────────────────────────────────────
PM_MAX_CANDIDATES = 8
PM_MAX_PER_SETUP = 8
PM_MEMO_HISTORY = 4

# ── Setup health ─────────────────────────────────────────────────────────
SETUP_REVIEW_MIN_TRADES = 12    # this account will never have many trades
