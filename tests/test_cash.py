"""CashMoney profile tests. Run: python -m unittest -v tests.test_cash

These run in the same process as the Luck tests, so the profile is loaded in
setUpModule and unloaded in tearDownModule. If a test here leaks the cash
rulebook into the Luck tests, everything downstream is wrong, so the first
test class checks the restore itself.
"""
import importlib
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CLAUDIO_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from engine import cashcore, indicators, risk, setups  # noqa: E402
from engine import rules as R  # noqa: E402
from engine.risk import Holding, Snapshot  # noqa: E402

LUCK_RISK = R.RISK_PER_TRADE


def setUpModule():
    os.environ["CLAUDIO_PROFILE"] = "cash"
    importlib.reload(R)
    from engine import db
    db.init()          # cash_review writes to the journal; the temp database needs its schema


def tearDownModule():
    os.environ.pop("CLAUDIO_PROFILE", None)
    importlib.reload(R)


def snap(**kw):
    base = dict(equity=100_000.0, settled_cash=100_000.0, holdings=[], regime="full", halt="ok")
    base.update(kw)
    return Snapshot(**base)


def frame(closes, vol=5_000_000, start="2024-01-01", spread=0.005):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c * (1 + spread),
                         "low": c * (1 - spread), "close": c, "volume": float(vol)}, index=idx)


def rising(n=320, start=50.0, step=0.06):
    """A long, steady uptrend: price above a rising 200-day average."""
    return frame([start + step * i for i in range(n)])


def falling(n=320, start=120.0, step=-0.10):
    return frame([max(5.0, start + step * i) for i in range(n)])


FUND = dict(peRatio=10.0, revChangeTTM=6.0, epsChangePercentTTM=8.0, totalDebtToEquity=60.0,
            returnOnEquity=18.0, currentRatio=1.8,
            netProfitMarginTTM=12.0, netProfitMarginMRQ=12.5)
SECTOR_PE = 20.0


# ── the overlay itself ───────────────────────────────────────────────────
class Overlay(unittest.TestCase):
    def test_the_cash_rulebook_is_loaded(self):
        self.assertEqual(R.PROFILE, "cash")
        self.assertEqual(R.RISK_PER_TRADE, 0.02)
        self.assertEqual(R.TOTAL_DRAWDOWN_HALT, 0.08)
        self.assertEqual(R.MAX_OPEN_RISK, 0.08)
        self.assertFalse(R.SETUPS_ENABLED["momentum"])
        self.assertTrue(R.SETUPS_ENABLED["value"])

    def test_it_unloads_cleanly(self):
        os.environ.pop("CLAUDIO_PROFILE", None)
        importlib.reload(R)
        try:
            self.assertEqual(R.RISK_PER_TRADE, LUCK_RISK)
            self.assertEqual(R.PROFILE, "luck")
            self.assertIsNone(R.CORE)
            self.assertTrue(R.SETUPS_ENABLED["momentum"])
        finally:
            os.environ["CLAUDIO_PROFILE"] = "cash"
            importlib.reload(R)

    def test_the_core_is_schx_under_a_monthly_trend_rule(self):
        # The owner handed the index core to the engine on 2026-09-25. It is governed by a
        # monthly close against the 200-day average, with an asymmetric re-entry: free to
        # leave, has to clear the line by 2% to come back. Backtested 2015-2026 on SCHX at
        # 10.39% CAGR and a -13.9% max drawdown, against 13.59% / -34.3% for buy and hold.
        self.assertEqual(R.CORE["symbol"], "SCHX")
        self.assertEqual(R.CORE["trend_ma"], 200)
        self.assertEqual(R.CORE["reentry_buffer"], 0.02)
        self.assertEqual(R.CORE["target_pct"], 0.50)

    def test_total_heat_stays_small_against_the_whole_account(self):
        # Unlike Luck, total heat here CAN exceed the sleeve's own halt: the inherited
        # holdings were sized by the owner, not by the rulebook. What has to stay small is
        # the number that matters -- risk measured against the whole account. The sleeve is
        # about a third of it, so 16% of the sleeve is ~5% of the account.
        sleeve_share_of_account = 0.32
        total = (R.MAX_OPEN_RISK + R.LEGACY["heat_budget"]) * sleeve_share_of_account
        self.assertLessEqual(total, 0.06, f"{total:.1%} of the account at risk is too much")


# ── sizing ───────────────────────────────────────────────────────────────
class Sizing(unittest.TestCase):
    def test_two_percent_of_the_sleeve_at_risk(self):
        qty, why = risk.size_trade(snap(), "ABC", "value", entry=50.0, stop=40.0,
                                   sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 200)               # $2,000 risk / $10 per share
        self.assertIn("2.00%", why)

    def test_fifteen_percent_position_cap_binds(self):
        qty, why = risk.size_trade(snap(), "ABC", "value", entry=50.0, stop=49.0,
                                   sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 300)               # $15,000 cap / $50, not $2,000/$1 = 2000
        self.assertIn("position cap", why)

    def test_eight_percent_heat_ceiling(self):
        held = [Holding(f"H{i}", qty=200, price=50.0, stop=40.0, setup="value")
                for i in range(4)]              # 4 x $2,000 = $8,000 = 8% of 100k
        qty, why = risk.size_trade(snap(holdings=held), "ABC", "value", entry=50.0,
                                   stop=40.0, sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0)
        self.assertIn("open-risk budget full", why)

    def test_one_new_position_a_day(self):
        qty, why = risk.size_trade(snap(entries_today=1), "ABC", "value", entry=50.0,
                                   stop=45.0, sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0)
        self.assertIn("daily entry limit", why)

    def test_four_new_positions_a_month(self):
        qty, why = risk.size_trade(snap(entries_this_month=4), "ABC", "value", entry=50.0,
                                   stop=45.0, sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0)
        self.assertIn("monthly entry limit", why)

    def test_eight_satellites_is_the_limit(self):
        held = [Holding(f"H{i}", qty=1, price=50.0, stop=49.9, setup="value") for i in range(8)]
        qty, why = risk.size_trade(snap(holdings=held), "ABC", "value", entry=50.0,
                                   stop=45.0, sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0)
        self.assertIn("max positions", why)

    def test_the_core_is_not_a_satellite_and_not_heat(self):
        core = Holding("VOO", qty=100, price=500.0, stop=None, setup="core")
        s = snap(holdings=[core] + [Holding(f"H{i}", qty=1, price=50.0, stop=49.9, setup="value")
                                    for i in range(7)])
        self.assertAlmostEqual(s.open_risk(), 7 * 0.1)  # the core contributes nothing
        qty, why = risk.size_trade(s, "ABC", "value", entry=50.0, stop=45.0,
                                   sector="Tech", market_cap=2e10)
        self.assertGreater(qty, 0, why)                 # 8th satellite still allowed

    def test_settled_cash_still_binds(self):
        thin = snap(equity=100_000.0, settled_cash=500.0)
        qty, why = risk.size_trade(thin, "ABC", "value", entry=50.0, stop=40.0,
                                   sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0, why)

    def test_a_real_trade_clears_the_minimum_position(self):
        # the sleeve is small; a rulebook that rejects every trade as "too small" is broken
        sleeve = snap(equity=11_300.0, settled_cash=11_300.0)
        qty, why = risk.size_trade(sleeve, "ABC", "value", entry=50.0, stop=42.5,
                                   sector="Tech", market_cap=2e10)
        self.assertGreater(qty * 50.0, R.MIN_POSITION_USD, why)

    def test_momentum_is_switched_off(self):
        qty, why = risk.size_trade(snap(), "ABC", "momentum", entry=50.0, stop=45.0,
                                   sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0)
        self.assertIn("not allowed", why)

    def test_no_new_buying_when_the_index_is_below_its_200_day(self):
        qty, why = risk.size_trade(snap(regime="risk_off"), "ABC", "value", entry=50.0,
                                   stop=45.0, sector="Tech", market_cap=2e10)
        self.assertEqual(qty, 0)


# ── the screen ───────────────────────────────────────────────────────────
class Screen(unittest.TestCase):
    def test_cheap_and_rising_qualifies(self):
        d = indicators.enrich(rising())
        c = setups.scan_value("ABC", d, FUND, SECTOR_PE)
        self.assertIsNotNone(c)
        self.assertEqual(c["setup"], "value")
        self.assertLess(c["stop"], c["trigger"])
        self.assertLessEqual((c["trigger"] - c["stop"]) / c["trigger"], R.VALUE["max_stop_pct"] + 1e-9)

    def test_cheap_but_below_the_200_day_is_rejected(self):
        d = indicators.enrich(falling())
        self.assertIsNone(setups.scan_value("ABC", d, FUND, SECTOR_PE))

    def test_above_a_falling_200_day_is_rejected(self):
        # long decline, then a bounce that clears the (still falling) average
        closes = [200 - 0.4 * i for i in range(300)] + [95 + 1.2 * i for i in range(20)]
        d = indicators.enrich(frame(closes))
        t = d.iloc[-1]
        self.assertGreater(t.close, t.ma200)                     # above the line...
        self.assertLess(t.ma200, d["ma200"].iloc[-22])           # ...but the line is falling
        self.assertIsNone(setups.scan_value("ABC", d, FUND, SECTOR_PE))

    def test_far_above_the_trend_is_momentum_not_value(self):
        closes = [50 + 0.02 * i for i in range(300)] + [56 * (1.03 ** i) for i in range(20)]
        d = indicators.enrich(frame(closes))
        self.assertGreater(d.iloc[-1].close, d.iloc[-1].ma200 * 1.30)
        self.assertIsNone(setups.scan_value("ABC", d, FUND, SECTOR_PE))

    def test_expensive_is_rejected_however_good_the_trend(self):
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value("ABC", d, dict(FUND, peRatio=19.0), SECTOR_PE))

    def test_shrinking_earnings_are_rejected(self):
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value("ABC", d, dict(FUND, epsChangePercentTTM=-1.0), SECTOR_PE))

    def test_too_much_debt_is_rejected(self):
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value("ABC", d, dict(FUND, totalDebtToEquity=200.0), SECTOR_PE))

    # ── the peak-cycle guards, each against the real shape of the name it was built for ──

    def test_priced_for_collapse_is_rejected(self):
        """MFG: a bank at a P/E of 3.2 against a sector median of 12.9. That is not a
        bargain the market missed, it is the market pricing in something."""
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value("MFG", d, dict(FUND, peRatio=3.2), 12.9))

    def test_earnings_that_multiplied_are_rejected(self):
        """CGAU: EPS +704% after gold ran. The P/E of 7 is arithmetic, not value."""
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value(
            "CGAU", d, dict(FUND, peRatio=7.2, epsChangePercentTTM=704.0), 18.7))

    def test_a_margin_already_rolling_over_is_rejected(self):
        """CGAU again: trailing net margin 36.9%, most recent quarter 16.3%. The trailing
        earnings the multiple rests on are already gone."""
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value(
            "CGAU", d, dict(FUND, netProfitMarginTTM=36.9, netProfitMarginMRQ=16.3), 18.7))

    def test_a_business_that_cannot_earn_on_its_capital_is_rejected(self):
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value("ABC", d, dict(FUND, returnOnEquity=3.0), SECTOR_PE))

    def test_a_weak_balance_sheet_is_rejected(self):
        """HMY: current ratio 0.84."""
        d = indicators.enrich(rising())
        self.assertIsNone(setups.scan_value("HMY", d, dict(FUND, currentRatio=0.84), SECTOR_PE))

    def test_a_missing_figure_does_not_silently_reject_everything(self):
        """Schwab omits the current ratio for banks and ROE for some names. A screen that
        rejects on absence quietly shrinks the universe to nothing."""
        d = indicators.enrich(rising())
        thin = {k: v for k, v in FUND.items()
                if k not in ("currentRatio", "returnOnEquity",
                             "netProfitMarginTTM", "netProfitMarginMRQ")}
        self.assertIsNotNone(setups.scan_value("ABC", d, thin, SECTOR_PE))

    def test_a_name_that_survives_everything_still_qualifies(self):
        """KALU: the one of the eight flagged names that the tightened screen keeps."""
        d = indicators.enrich(rising())
        kalu = dict(peRatio=11.1, revChangeTTM=33.0, epsChangePercentTTM=68.0,
                    totalDebtToEquity=112.3, returnOnEquity=26.4, currentRatio=2.50,
                    netProfitMarginTTM=5.5, netProfitMarginMRQ=7.7)
        self.assertIsNotNone(setups.scan_value("KALU", d, kalu, 28.7))


# ── exits ────────────────────────────────────────────────────────────────
def pos(**kw):
    base = dict(symbol="ABC", setup="value", entry_price=100.0, qty=100, stop=90.0,
                risk_per_share=10.0, entry_date="2026-01-05", high_water=100.0,
                partial_done=0, max_exit_date=None, legacy=0)
    base.update(kw)
    return base


class Exits(unittest.TestCase):
    def test_earnings_never_force_a_sale(self):
        acts = risk.exit_signals(pos(), 95.0, dict(close=95.0), date(2026, 3, 2), 100_000.0,
                                 next_earnings=date(2026, 3, 3))
        self.assertNotIn("exit", [a for a, _ in acts])

    def test_breakeven_stop_at_one_R_on_a_close(self):
        acts = risk.exit_signals(pos(), 111.0, dict(close=111.0, ma200=80.0),
                                 date(2026, 3, 2), 100_000.0)
        self.assertIn(("raise_stop", 100.0), acts)

    def test_the_stop_trails_two_percent_under_the_200_day(self):
        acts = risk.exit_signals(pos(), 150.0, dict(close=150.0, ma200=120.0),
                                 date(2026, 3, 2), 100_000.0)
        self.assertIn(("raise_stop", round(120.0 * 0.98, 2)), acts)

    def test_the_stop_never_moves_down(self):
        acts = risk.exit_signals(pos(stop=118.0), 150.0, dict(close=150.0, ma200=100.0),
                                 date(2026, 3, 2), 100_000.0)
        self.assertEqual([a for a, _ in acts if a == "raise_stop"], [])

    def test_no_partial_sales(self):
        acts = risk.exit_signals(pos(), 200.0, dict(close=200.0, ma200=150.0),
                                 date(2026, 3, 2), 100_000.0)
        self.assertNotIn("partial", [a for a, _ in acts])

    def test_no_time_stop(self):
        old = pos(entry_date="2025-06-02")
        acts = risk.exit_signals(old, 101.0, dict(close=101.0, ma200=90.0),
                                 date(2026, 3, 2), 100_000.0)
        self.assertNotIn("exit", [a for a, _ in acts])


# ── the index core ───────────────────────────────────────────────────────
DORMANT_CORE = dict(symbol="VOO", target_pct=0.45, trend_ma=200, reentry_buffer=0.02,
                    rebalance_band=0.10, min_trade_usd=500)


class Core(unittest.TestCase):
    """The core is live in this profile (SCHX). These inject a different core so the tests
    read against fixed numbers rather than whatever the profile happens to say today."""

    def setUp(self):
        self.today = date(2026, 3, 10)
        self._saved, R.CORE = R.CORE, DORMANT_CORE

    def tearDown(self):
        R.CORE = self._saved

    def test_with_no_core_configured_it_does_nothing(self):
        R.CORE = None
        p = cashcore.core_plan(None, self.today, 0, 100.0, 100_000.0)
        self.assertEqual(p["action"], "hold")
        self.assertIn("no core", p["reason"])

    def test_buys_the_core_when_the_month_closes_above_the_line(self):
        d = indicators.enrich(frame([300 + 0.3 * i for i in range(400)], start="2024-06-03"))
        p = cashcore.core_plan(d, self.today, held_qty=0, price=400.0, equity=100_000.0)
        self.assertEqual(p["action"], "buy")
        self.assertEqual(p["qty"], int(100_000.0 * DORMANT_CORE["target_pct"] // 400))
        self.assertIn("uptrend", p["reason"])

    def test_sells_the_core_when_the_month_closes_below_the_line(self):
        closes = [300 + 0.3 * i for i in range(330)] + [400 - 1.6 * i for i in range(70)]
        d = indicators.enrich(frame(closes, start="2024-06-03"))
        p = cashcore.core_plan(d, self.today, held_qty=125, price=290.0, equity=100_000.0)
        self.assertEqual(p["action"], "sell")
        self.assertEqual(p["qty"], 125)
        self.assertIn("trend broken", p["reason"])

    def test_the_re_entry_buffer_keeps_it_out_just_above_the_line(self):
        self.assertTrue(cashcore.wants_in(100.5, 100.0, currently_in=True))
        self.assertFalse(cashcore.wants_in(100.5, 100.0, currently_in=False))
        self.assertTrue(cashcore.wants_in(102.5, 100.0, currently_in=False))

    def test_small_drift_is_left_alone(self):
        d = indicators.enrich(frame([300 + 0.3 * i for i in range(400)], start="2024-06-03"))
        target = int(100_000.0 * DORMANT_CORE["target_pct"] // 400)
        p = cashcore.core_plan(d, self.today, held_qty=target + 5, price=400.0, equity=100_000.0)
        self.assertEqual(p["action"], "hold")
        self.assertIn("leave it alone", p["reason"])

    def test_big_drift_is_trimmed(self):
        d = indicators.enrich(frame([300 + 0.3 * i for i in range(400)], start="2024-06-03"))
        target = int(100_000.0 * DORMANT_CORE["target_pct"] // 400)
        p = cashcore.core_plan(d, self.today, held_qty=200, price=400.0, equity=100_000.0)
        self.assertEqual(p["action"], "trim")
        self.assertEqual(p["qty"], 200 - target)

    def test_it_only_decides_once_a_month(self):
        d = indicators.enrich(frame([300 + 0.3 * i for i in range(400)], start="2024-06-03"))
        first = cashcore.core_plan(d, self.today, 0, 400.0, 100_000.0)
        again = cashcore.core_plan(d, self.today, 0, 400.0, 100_000.0,
                                   last_month_done=first["month"])
        self.assertEqual(again["action"], "hold")
        self.assertIn("already done", again["reason"])

    def test_it_waits_rather_than_guessing_without_history(self):
        d = indicators.enrich(frame([100.0] * 60, start="2026-01-01"))
        p = cashcore.core_plan(d, self.today, 0, 100.0, 100_000.0)
        self.assertEqual(p["action"], "wait")

    def test_a_monthly_close_below_the_200_day_breaks_a_satellite(self):
        up = indicators.enrich(frame([50 + 0.06 * i for i in range(400)], start="2024-06-03"))
        self.assertFalse(cashcore.trend_break(up, self.today))
        closes = [200 - 0.4 * i for i in range(300)] + [80.0] * 100
        down = indicators.enrich(frame(closes, start="2024-06-03"))
        self.assertTrue(cashcore.trend_break(down, self.today))

    def test_idle_cash_is_reported_not_swept(self):
        note = cashcore.idle_cash_note(50_000)
        self.assertIn("50,000", note)
        self.assertIn("money market", note)
        self.assertEqual(cashcore.idle_cash_note(200), "")


# ── the core's cash, and whose book is whose ─────────────────────────────
class CoreReserve(unittest.TestCase):
    """A trend break raises a large pile of cash at a market low. That is exactly the
    moment the stock picker must not be handed it."""

    def setUp(self):
        self._saved, R.CORE = R.CORE, DORMANT_CORE

    def tearDown(self):
        R.CORE = self._saved

    def test_an_invested_core_reserves_nothing(self):
        # its money is in the core, not in cash
        self.assertEqual(cashcore.core_cash_reserve("hold", 0, 30.0, 580, 35_000.0), 0.0)

    def test_a_purchase_reserves_exactly_its_cost(self):
        self.assertEqual(cashcore.core_cash_reserve("buy", 100, 30.0, 0, 35_000.0), 3_000.0)

    def test_a_core_sitting_out_ring_fences_its_whole_target(self):
        got = cashcore.core_cash_reserve("hold", 0, 30.0, 0, 35_000.0)
        self.assertEqual(got, 35_000.0 * DORMANT_CORE["target_pct"])

    def test_a_profile_with_no_core_reserves_nothing(self):
        R.CORE = None
        self.assertEqual(cashcore.core_cash_reserve("hold", 0, 30.0, 0, 35_000.0), 0.0)

    def test_the_picker_cannot_spend_the_cash_the_core_just_raised(self):
        reserve = cashcore.core_cash_reserve("hold", 0, 30.0, 0, 35_000.0)
        s = snap(equity=35_000.0, settled_cash=max(0.0, 22_000.0 - reserve))
        self.assertLess(s.settled_cash, 22_000.0 - 15_000.0)


class Book(unittest.TestCase):
    """Fenced sleeves are the owner's, not the engine's. They carry no stop by design, and
    a stopless holding counts its whole value as open risk."""

    def _acct(self, excluded=("SGOV", "SCHA")):
        return dict(excluded=list(excluded), positions={
            "SCHX": dict(qty=580, price=30.27),
            "SGOV": dict(qty=45, price=100.65),
            "SCHA": dict(qty=57, price=33.09),
            "HOLDB": dict(qty=10, price=124.00)})

    def _book(self, acct):
        from engine import cashrun
        return cashrun._book(acct, {}, "SCHX")

    def test_fenced_sleeves_are_left_out(self):
        self.assertEqual({h.symbol for h in self._book(self._acct())}, {"SCHX", "HOLDB"})

    def test_the_core_is_tagged_core_and_carries_no_heat(self):
        core = next(h for h in self._book(self._acct()) if h.symbol == "SCHX")
        self.assertEqual(core.setup, "core")
        self.assertEqual(core.open_risk, 0.0, "the core answers to the trend rule, not a stop")

    def test_leaving_them_in_would_block_every_entry(self):
        # $6.4k of stopless fenced ETFs on the account is 18% of equity against an 8%
        # budget: the screen would run every night and never be able to buy anything.
        wide = risk.Snapshot(equity=35_190.0, settled_cash=5_100.0,
                             holdings=self._book(self._acct(excluded=())))
        self.assertGreater(wide.open_risk() / wide.equity, R.MAX_OPEN_RISK)
        tight = risk.Snapshot(equity=35_190.0, settled_cash=5_100.0,
                              holdings=self._book(self._acct()))
        self.assertLess(tight.open_risk() / tight.equity, R.MAX_OPEN_RISK)


# ── the memo ─────────────────────────────────────────────────────────────
class Memo(unittest.TestCase):
    def test_it_says_plainly_that_it_cannot_trade_yet(self):
        from engine import cashrun
        p = dict(today="2026-09-24", regime="full", screened=0, candidates=[], breaks=[],
                 idle=0.0, idle_note="", core_price=500.0,
                 core=dict(action="wait", qty=0, reason="not enough history"),
                 acct=dict(live=False, equity=25_000.0, cash=25_000.0, last4=None,
                           excluded=["HOLDA", "HOLDC"], positions={}))
        out = cashrun.render(p)
        self.assertIn("ADVISORY ONLY", out)
        self.assertIn("No order can be placed", out)
        self.assertIn("HOLDA", out)
        self.assertIn("Nothing qualifies today", out)
        self.assertIn(f"-{R.TOTAL_DRAWDOWN_HALT:.0%} from peak", out)


class Review(unittest.TestCase):
    """The portfolio manager is the last gate. What matters is that a failed call blocks
    buying rather than quietly falling through to the raw screen."""

    def _plan(self, **kw):
        base = dict(today="2026-09-25", regime="full", screened=12, breaks=[], idle=0.0,
                    idle_note="", core_price=None, capacity=1, reviewed=True,
                    pm=None, pm_error=None,
                    core=dict(action="hold", qty=0, reason="no core in this profile"),
                    acct=dict(live=True, equity=11_300.0, cash=5_000.0, last4="0042",
                              excluded=[], positions={}),
                    candidates=[dict(symbol="KALU", trigger=155.10, stop=140.03, qty=8,
                                     cost=1240.0, risk=120.0, note="P/E 11.1 vs sector 28.7",
                                     why="")])
        base.update(kw)
        return base

    def test_a_failed_review_blocks_buying_and_says_so(self):
        from engine import cashrun
        out = cashrun.render(self._plan(pm_error="the portfolio manager call failed, so "
                                                 "nothing is approved tonight."))
        self.assertIn("failed", out)
        self.assertIn("nothing is approved", out)

    def test_an_approval_names_the_thesis_and_what_breaks_it(self):
        from engine import cashrun
        pm = dict(memo="One name worth having.", entries=[dict(
            symbol="KALU", size_mult=1.0, thesis="Cheap against its sector with margins still expanding.",
            invalidation="A close below the 200-day average.")], vetoes=[], holdings=[], themes=[])
        out = cashrun.render(self._plan(pm=pm))
        self.assertIn("APPROVED", out)
        self.assertIn("KALU", out)
        self.assertIn("invalidated if", out)

    def test_approving_nothing_is_stated_plainly(self):
        from engine import cashrun
        pm = dict(memo="Nothing this week.", entries=[], holdings=[], themes=[],
                  vetoes=[dict(symbol="CGAU", reason="Gold miner at peak earnings.")])
        out = cashrun.render(self._plan(pm=pm))
        self.assertIn("APPROVED: nothing", out)
        self.assertIn("VETOED CGAU", out)
        self.assertIn("peak earnings", out)

    def test_the_review_validates_what_the_model_returns(self):
        """The model's reply is untrusted input. Only symbols it was actually shown, and
        only within capacity, may reach an order ticket."""
        from engine import analyst
        import json as _json
        reply = _json.dumps(dict(
            memo="Alphabet is the only one worth owning here.",
            entries=[dict(symbol="GOOGL", size="full", thesis="Half the sector multiple.",
                          invalidation="A monthly close below the 200-day."),
                     dict(symbol="NVDA", size="full", thesis="Never shown to it.",
                          invalidation="n/a")],
            vetoes=[dict(symbol="KALU", reason="Aluminium at a cycle high.")],
            holdings=[], themes=["cheap megacap"]))
        payload = dict(
            candidates=[dict(symbol="GOOGL"), dict(symbol="KALU")],
            holdings=[], account=dict(capacity=1))
        real = analyst._call
        analyst._call = lambda *a, **k: reply
        try:
            out = analyst.cash_review(payload)
        finally:
            analyst._call = real
        self.assertEqual([e["symbol"] for e in out["entries"]], ["GOOGL"],
                         "NVDA was never shown to the model and must not survive validation")
        self.assertEqual(out["vetoes"][0]["symbol"], "KALU")
        self.assertIn("Alphabet", out["memo"])

    def test_a_thrown_call_returns_none_rather_than_guessing(self):
        from engine import analyst
        real = analyst._call

        def boom(*a, **k):
            raise RuntimeError("daily LLM call cap reached")

        analyst._call = boom
        try:
            out = analyst.cash_review(dict(candidates=[], holdings=[],
                                           account=dict(capacity=1)))
        finally:
            analyst._call = real
        self.assertIsNone(out, "a failed review must block buying, not fall through")

    def test_the_cash_profile_answers_to_its_own_policy_document(self):
        from engine import analyst
        self.assertIn("STRATEGY_CASH.md", analyst.strategy_context.__code__.co_consts
                      + tuple(str(c) for c in analyst.strategy_context.__code__.co_consts))


# ── shared market data ───────────────────────────────────────────────────
class MarketData(unittest.TestCase):
    """Bars and fundamentals are the same facts for both profiles, so the cash engine
    borrows the Luck engine's copy instead of keeping its own and refreshing it twice."""

    def _reload(self, **env):
        from engine import db
        old = {k: os.environ.get(k) for k in ("CLAUDIO_MARKET_DB", "CLAUDIO_MARKET_READONLY")}
        for k, v in env.items():
            os.environ[k] = v if v is not None else ""
            if v is None:
                os.environ.pop(k, None)
        try:
            importlib.reload(db)
            return db, dict(market=db.MARKET_DB_PATH, readonly=db.MARKET_READONLY,
                            own=db.DB_PATH)
        finally:
            pass

    def _restore(self):
        from engine import db
        for k in ("CLAUDIO_MARKET_DB", "CLAUDIO_MARKET_READONLY"):
            os.environ.pop(k, None)
        importlib.reload(db)

    def test_unset_keeps_everything_in_one_database(self):
        try:
            db, info = self._reload(CLAUDIO_MARKET_DB=None, CLAUDIO_MARKET_READONLY=None)
            self.assertEqual(info["market"], info["own"])
            self.assertFalse(info["readonly"])
        finally:
            self._restore()

    def test_a_borrowing_profile_cannot_write_to_the_lenders_data(self):
        try:
            db, info = self._reload(CLAUDIO_MARKET_DB="/tmp/not-ours.db",
                                    CLAUDIO_MARKET_READONLY="1")
            self.assertEqual(info["market"], "/tmp/not-ours.db")
            self.assertNotEqual(info["market"], info["own"])
            for fn, args in ((db.save_bars, ("ABC", [])),
                             (db.save_universe, ([],)),
                             (db.save_fundamentals, ("ABC", {}))):
                with self.assertRaises(RuntimeError) as e:
                    fn(*args)
                self.assertIn("must not write to it", str(e.exception))
        finally:
            self._restore()


# ── the fence ────────────────────────────────────────────────────────────
class Fence(unittest.TestCase):
    def _config(self, **env):
        import schwab_api.config as cfg
        old = {k: os.environ.get(k) for k in
               ("CLAUDIO_PROFILE", "CLAUDIO_ACCOUNTS", "CLAUDIO_TRADING", "CLAUDIO_EXCLUDED")}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
        try:
            importlib.reload(cfg)
            # snapshot the values: the finally block reloads the module back to normal,
            # and the module object it would return is the same one.
            return type("cfg", (), dict(ALLOWED_ACCOUNTS=set(cfg.ALLOWED_ACCOUNTS),
                                        EXCLUDED_SYMBOLS=set(cfg.EXCLUDED_SYMBOLS),
                                        TRADING_ENABLED=cfg.TRADING_ENABLED,
                                        TOKEN_PATH=cfg.TOKEN_PATH))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.update({k: v})
            importlib.reload(cfg)

    def test_a_profile_with_no_account_granted_can_touch_nothing(self):
        c = self._config(CLAUDIO_PROFILE="cash", CLAUDIO_ACCOUNTS=None, CLAUDIO_TRADING=None)
        self.assertEqual(c.ALLOWED_ACCOUNTS, set())
        self.assertFalse(c.TRADING_ENABLED)

    def test_a_profile_never_inherits_the_luck_account(self):
        # even with config_local naming Luck, the cash profile only sees what it is given
        c = self._config(CLAUDIO_PROFILE="cash", CLAUDIO_ACCOUNTS="0042", CLAUDIO_TRADING=None)
        self.assertEqual(c.ALLOWED_ACCOUNTS, {"0042"})
        self.assertFalse(c.TRADING_ENABLED)

    def test_trading_needs_an_explicit_yes(self):
        c = self._config(CLAUDIO_PROFILE="cash", CLAUDIO_ACCOUNTS="0042", CLAUDIO_TRADING="0")
        self.assertFalse(c.TRADING_ENABLED)
        c = self._config(CLAUDIO_PROFILE="cash", CLAUDIO_ACCOUNTS="0042", CLAUDIO_TRADING="1")
        self.assertTrue(c.TRADING_ENABLED)

    def test_the_owners_own_holdings_are_excluded(self):
        c = self._config(CLAUDIO_PROFILE="cash", CLAUDIO_ACCOUNTS="0042",
                         CLAUDIO_EXCLUDED="holda,holdb,holdc,holdd")
        self.assertEqual(c.EXCLUDED_SYMBOLS, {"HOLDA", "HOLDB", "HOLDC", "HOLDD"})


if __name__ == "__main__":
    unittest.main()
