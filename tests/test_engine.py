"""Unit tests for the rules that must never break. Run: python -m unittest -v tests.test_engine"""
import math
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CLAUDIO_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

from engine import indicators, risk, setups  # noqa: E402
from engine import rules as R  # noqa: E402
from engine.risk import Holding, Snapshot  # noqa: E402


def snap(**kw):
    base = dict(equity=10_000.0, settled_cash=10_000.0, holdings=[], regime="full", halt="ok")
    base.update(kw)
    return Snapshot(**base)


def frame(closes, vol=1_000_000, start="2025-01-01", spread=0.01):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c * (1 + spread),
                         "low": c * (1 - spread), "close": c, "volume": float(vol)}, index=idx)


class Sizing(unittest.TestCase):
    # These use "catalyst": sizing maths is setup-independent, but momentum currently
    # trades at half size (SETUP_SIZE_MULT), which is asserted in test_setup_probation.
    def test_risk_budget(self):
        qty, _ = risk.size_trade(snap(), "AAA", "catalyst", 100, 95, "Tech", 50e9)
        # 1.5% of 10k = 150 risk / $5 per share = 30 sh, $3,000 > 20% cap ($2,000) -> 20 sh
        self.assertEqual(qty, 20)

    def test_wide_stop_uses_risk_not_cap(self):
        qty, why = risk.size_trade(snap(), "AAA", "catalyst", 100, 90, "Tech", 50e9)
        self.assertEqual(qty, 15)            # 150/10 = 15 sh, $1,500, inside the $2,000 cap
        qty, why = risk.size_trade(snap(), "AAA", "catalyst", 100, 80, "Tech", 50e9)
        self.assertEqual(qty, 7)             # 150/20 = 7.5 -> 7

    def test_smallcap_cap(self):
        qty, why = risk.size_trade(snap(), "SML", "catalyst", 50, 48, "Tech", 1e9)
        self.assertEqual(qty, 20)            # 10% of 10k = $1,000 / $50
        self.assertIn("position cap", why)

    def test_settled_cash_binds(self):
        qty, why = risk.size_trade(snap(settled_cash=500), "AAA", "catalyst", 100, 95, "Tech", 50e9)
        self.assertEqual(qty, 5)
        self.assertIn("settled cash", why)
        qty, why = risk.size_trade(snap(settled_cash=200), "AAA", "catalyst", 100, 95, "Tech", 50e9)
        self.assertEqual(qty, 0)             # below $300 minimum

    def test_reserved_cash_counts(self):
        qty, _ = risk.size_trade(snap(settled_cash=1000, reserved_cash=800), "AAA", "catalyst",
                                 100, 95, "Tech", 50e9)
        self.assertEqual(qty, 0)

    def test_heat_budget(self):
        hs = [Holding("H1", 100, 10, 7.0, "Tech", 50e9), Holding("H2", 100, 10, 6.0, "Fin", 50e9)]
        # open risk = 300 + 400 = 700 = 7.0% -> room 50 < half budget 75 -> reject
        qty, why = risk.size_trade(snap(holdings=hs), "AAA", "catalyst", 100, 95, "Energy", 50e9)
        self.assertEqual(qty, 0)
        self.assertIn("open-risk", why)

    def test_unprotected_holding_counts_full(self):
        hs = [Holding("OLD", 10, 100, None, "Tech", 50e9)]
        self.assertEqual(hs[0].open_risk, 1000)

    def test_sector_cap(self):
        hs = [Holding("T1", 30, 100, 99, "Tech", 50e9)]   # $3,000 tech, tiny risk
        qty, why = risk.size_trade(snap(holdings=hs), "AAA", "catalyst", 100, 95, "Tech", 50e9)
        self.assertEqual(qty, 5)             # 35% = 3,500 - 3,000 = 500 -> 5 sh
        self.assertIn("sector", why)

    def test_limits(self):
        full = [Holding(f"S{i}", 1, 10, 9.99, "X", 50e9) for i in range(R.MAX_POSITIONS)]
        self.assertEqual(risk.size_trade(snap(holdings=full), "AAA", "catalyst", 100, 95, "T", 50e9)[0], 0)
        self.assertEqual(risk.size_trade(snap(entries_today=3), "AAA", "catalyst", 100, 95, "T", 50e9)[0], 0)
        self.assertEqual(risk.size_trade(snap(halt="day_halt"), "AAA", "catalyst", 100, 95, "T", 50e9)[0], 0)
        self.assertEqual(risk.size_trade(snap(), "AAA", "catalyst", 100, 101, "T", 50e9)[0], 0)
        self.assertEqual(risk.size_trade(snap(), "AAA", "catalyst", 100, 95, "T", 50e9,
                                         cooldown_active=True)[0], 0)

    def test_regime(self):
        self.assertEqual(risk.size_trade(snap(regime="risk_off"), "A", "catalyst", 100, 95, "T", 50e9)[0], 0)
        q_full, _ = risk.size_trade(snap(), "A", "bounce", 100, 80, "T", 50e9)
        q_off, _ = risk.size_trade(snap(regime="risk_off"), "A", "bounce", 100, 80, "T", 50e9)
        self.assertEqual(q_off, math.floor(q_full / 2))

    def test_analyst_can_only_shrink(self):
        q1, _ = risk.size_trade(snap(), "A", "catalyst", 100, 80, "T", 50e9, size_mult=1.0)
        q2, _ = risk.size_trade(snap(), "A", "catalyst", 100, 80, "T", 50e9, size_mult=5.0)
        q3, _ = risk.size_trade(snap(), "A", "catalyst", 100, 80, "T", 50e9, size_mult=0.5)
        self.assertEqual(q1, q2)
        self.assertEqual(q3, 3)

    def test_max_single_loss_never_exceeds_budget(self):
        rng = np.random.default_rng(1)
        for _ in range(500):
            entry = float(rng.uniform(5, 500))
            stop = entry * float(rng.uniform(0.80, 0.995))
            eq = float(rng.uniform(2_000, 100_000))
            s = snap(equity=eq, settled_cash=eq)
            qty, _ = risk.size_trade(s, "Z", "catalyst", entry, stop, "T", float(rng.choice([5e8, 5e10])))
            self.assertLessEqual(qty * (entry - stop), eq * R.RISK_PER_TRADE + 1e-6)
            self.assertLessEqual(qty * entry, eq * R.MAX_POSITION_PCT + 1e-6)

    def test_setup_probation_halves_the_trade(self):
        # wide stop so the risk budget binds, not the position cap
        full, _ = risk.size_trade(snap(), "AAA", "catalyst", 100, 90, "Tech", 50e9)
        half, _ = risk.size_trade(snap(), "AAA", "momentum", 100, 90, "Tech", 50e9)
        self.assertEqual(R.SETUP_SIZE_MULT["momentum"], 0.5)
        self.assertEqual(half, math.floor(full * R.SETUP_SIZE_MULT["momentum"]))
        self.assertLessEqual(half * (100 - 90), 10_000 * R.RISK_PER_TRADE * 0.5 + 1e-6)


class KillSwitch(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(risk.kill_switch(9_600, 10_000, 10_000), "ok")
        self.assertEqual(risk.kill_switch(9_500, 10_000, 10_000), "day_halt")
        self.assertEqual(risk.kill_switch(9_000, 9_200, 10_000), "full_halt")


class Exits(unittest.TestCase):
    pos = dict(symbol="A", setup="momentum", entry_price=100, risk_per_share=5, stop=95, qty=30,
               partial_done=0, entry_date=str(date.today()), max_exit_date=None)

    def test_breakeven_needs_a_close_not_a_wick(self):
        # price spikes to +1.2R intraday but the last close was +0.4R: stop stays put
        acts = risk.exit_signals(self.pos, 106, {"close": 102}, date.today(), 10_000)
        self.assertFalse([a for a in acts if a[0] == "raise_stop"])
        acts = risk.exit_signals(self.pos, 106, {}, date.today(), 10_000)   # no bar yet (entry day)
        self.assertFalse([a for a in acts if a[0] == "raise_stop"])
        acts = risk.exit_signals(self.pos, 106, {"close": 105}, date.today(), 10_000)
        self.assertIn(("raise_stop", 100), acts)

    def test_partial_at_3r_takes_the_spike(self):
        self.assertFalse([a for a in risk.exit_signals(self.pos, 111, {}, date.today(), 10_000)
                          if a[0] == "partial"])                      # +2.2R is no longer enough
        acts = risk.exit_signals(self.pos, 116, {}, date.today(), 10_000)
        self.assertIn(("partial", 10), acts)


    def test_ma50_trail_needs_a_close_at_1r(self):
        p = dict(self.pos, setup="legacy")
        ctx = {"ma50": 98, "close": 102}                 # close +0.4R -> no trail yet
        self.assertFalse([a for a in risk.exit_signals(p, 106, ctx, date.today(), 10_000)
                          if a[0] == "raise_stop"])
        ctx = {"ma50": 105, "close": 106}                # close +1.2R -> trail under the MA50
        self.assertIn(("raise_stop", round(105 * 0.99, 2)),
                      risk.exit_signals(p, 106, ctx, date.today(), 10_000))

    def test_trail_never_lowers(self):
        acts = risk.exit_signals(self.pos, 101, {"low10": 90}, date.today(), 10_000)
        self.assertFalse([a for a in acts if a[0] == "raise_stop"])
        acts = risk.exit_signals(self.pos, 101, {"low10": 97}, date.today(), 10_000)
        self.assertIn(("raise_stop", 97), acts)

    def test_time_stop(self):
        p = dict(self.pos, entry_date=str(date.today() - timedelta(days=12)))
        self.assertEqual(risk.exit_signals(p, 101, {}, date.today(), 10_000)[0][0], "exit")
        self.assertNotEqual((risk.exit_signals(p, 106, {}, date.today(), 10_000) or [("x",)])[0][0], "exit")

    def test_earnings_exit(self):
        acts = risk.exit_signals(self.pos, 101, {}, date.today(), 10_000, date.today() + timedelta(days=1))
        self.assertEqual(acts[0][0], "exit")

    def test_bounce_exit_only_with_ma5(self):
        p = dict(self.pos, setup="bounce")
        self.assertEqual(risk.exit_signals(p, 101, {"ma5": 100.5}, date.today(), 10_000)[0][0], "exit")
        self.assertFalse([a for a in risk.exit_signals(p, 101, {}, date.today(), 10_000) if a[0] == "exit"])


class Runners(unittest.TestCase):
    # entry 100, 1R = $5. high_water 116 = +3.2R closed -> runner
    pos = dict(symbol="A", setup="momentum", entry_price=100, risk_per_share=5, stop=100, qty=30,
               partial_done=1, entry_date=str(date.today() - timedelta(days=30)),
               max_exit_date=str(date.today() - timedelta(days=5)), high_water=116)

    def test_runner_ignores_max_hold_and_time_stop(self):
        acts = risk.exit_signals(self.pos, 114, {"close": 114}, date.today(), 10_000)
        self.assertFalse([a for a in acts if a[0] == "exit"])
        swing = dict(self.pos, high_water=104)                        # never closed at +3R
        self.assertEqual(risk.exit_signals(swing, 114, {"close": 114}, date.today(), 10_000)[0],
                         ("exit", "max hold reached"))

    def test_runner_trails_the_20_day_low(self):
        ctx = {"low10": 108, "low20": 104, "close": 116}
        self.assertIn(("raise_stop", 104), risk.exit_signals(self.pos, 116, ctx, date.today(), 10_000))
        swing = dict(self.pos, high_water=104, max_exit_date=None, entry_date=str(date.today()))
        self.assertIn(("raise_stop", 108), risk.exit_signals(swing, 116, ctx, date.today(), 10_000))

    def test_runner_also_respects_the_50_day(self):
        ctx = {"low20": 104, "ma50": 112, "close": 116}
        self.assertIn(("raise_stop", round(112 * 0.99, 2)),
                      risk.exit_signals(self.pos, 116, ctx, date.today(), 10_000))


class EarningsCushion(unittest.TestCase):
    pos = dict(symbol="A", setup="momentum", entry_price=100, risk_per_share=5, stop=100, qty=30,
               partial_done=0, entry_date=str(date.today()), max_exit_date=None, high_water=112)
    tomorrow = date.today() + timedelta(days=1)

    def test_cushion_sells_half_and_holds_the_rest(self):
        acts = risk.exit_signals(self.pos, 112, {"close": 112}, date.today(), 10_000, self.tomorrow)
        self.assertEqual(acts, [("earnings_trim", 15)])

    def test_no_cushion_still_goes_flat(self):
        thin = dict(self.pos, high_water=104)                         # +0.8R close, stop at entry
        self.assertEqual(risk.exit_signals(thin, 104, {"close": 104}, date.today(), 10_000,
                                           self.tomorrow)[0][0], "exit")
        loose = dict(self.pos, stop=95)                               # cushion, but stop below entry
        self.assertEqual(risk.exit_signals(loose, 112, {"close": 112}, date.today(), 10_000,
                                           self.tomorrow)[0][0], "exit")

    def test_trims_once_then_holds(self):
        done = dict(self.pos, earnings_trimmed=True)
        acts = risk.exit_signals(done, 112, {"close": 112}, date.today(), 10_000, self.tomorrow)
        self.assertEqual(acts[0][0], "exit")                          # momentum: flat on the 2nd pass

    def test_bounce_never_holds_through_earnings(self):
        b = dict(self.pos, setup="bounce")
        self.assertEqual(risk.exit_signals(b, 112, {"close": 112}, date.today(), 10_000,
                                           self.tomorrow)[0][0], "exit")

class Setups(unittest.TestCase):
    def test_momentum_candidate(self):
        closes = list(np.linspace(50, 100, 260)) + [99.5] * 5
        d = indicators.enrich(frame(closes))
        c = setups.scan_momentum("MOM", d, 0.95)
        self.assertIsNotNone(c)
        self.assertGreater(c["trigger"], d["close"].iloc[-1])
        self.assertLess(c["stop"], c["trigger"])
        self.assertGreaterEqual(c["stop"], c["trigger"] * (1 - R.MOMENTUM["max_stop_pct"]) - 0.01)
        self.assertIsNone(setups.scan_momentum("MOM", d, 0.5))           # weak RS

    def test_no_momentum_in_downtrend(self):
        d = indicators.enrich(frame(list(np.linspace(100, 50, 260))))
        self.assertIsNone(setups.scan_momentum("DN", d, 0.99))

    def test_bounce_confirm(self):
        closes = list(np.linspace(50, 100, 250)) + [100, 97, 94, 92]
        d = indicators.enrich(frame(closes))
        self.assertIsNotNone(setups.scan_bounce("B", d))
        ok, stop, why = setups.bounce_confirm(d, 90.0)
        self.assertTrue(ok, why)
        self.assertLess(stop, 90.0)
        ok, _, _ = setups.bounce_confirm(d, 99.0)
        self.assertFalse(ok)

    def test_catalyst_needs_earnings(self):
        closes = list(np.linspace(50, 60, 250))
        df = frame(closes)
        gap_day = df.index[-1]
        df.loc[gap_day, ["open", "high", "low", "close", "volume"]] = [64, 67, 63.5, 66.5, 5_000_000]
        d = indicators.enrich(df)
        self.assertIsNone(setups.scan_catalyst("C", d, []))
        c = setups.scan_catalyst("C", d, [gap_day.date()])
        self.assertIsNotNone(c)
        self.assertLess(c["stop"], 63.5)

    def test_value_rules(self):
        f = dict(peRatio=10, revChangeTTM=12, epsChangePercentTTM=20, totalDebtToEquity=40)
        self.assertTrue(setups.value_fundamentals_ok(f, 20))
        self.assertFalse(setups.value_fundamentals_ok(dict(f, peRatio=19), 20))
        self.assertFalse(setups.value_fundamentals_ok(dict(f, revChangeTTM=1), 20))
        self.assertFalse(setups.value_fundamentals_ok(dict(f, totalDebtToEquity=400), 20))
        self.assertFalse(setups.value_fundamentals_ok(dict(f, peRatio=-5), 20))


if __name__ == "__main__":
    unittest.main()


class PMGuardrails(unittest.TestCase):
    cands = [dict(symbol="AAA", setup="momentum", trigger=100, stop=95),
             dict(symbol="BBB", setup="value", trigger=50, stop=45)]
    holds = [dict(symbol="HLD", setup="momentum", price=110, stop=100),
             dict(symbol="OLD", setup="legacy_exit", price=20, stop=17)]

    def run_pm(self, v, capacity=2):
        from engine.analyst import validate_pm
        return validate_pm(v, self.cands, self.holds, {"HLD": 110, "OLD": 20}, capacity)

    def test_cannot_invent_symbols_or_resize_up(self):
        clean, dropped = self.run_pm({"entries": [{"symbol": "ZZZ", "size": "full"},
                                                  {"symbol": "AAA", "size": "double"},
                                                  {"symbol": "AAA", "size": "half"}]})
        self.assertEqual([e["symbol"] for e in clean["entries"]], ["AAA"])
        self.assertEqual(clean["entries"][0]["size_mult"], 1.0)   # unknown size -> full, never more

    def test_stop_only_tightens_below_price(self):
        clean, _ = self.run_pm({"holdings": [{"symbol": "HLD", "action": "tighten", "new_stop": 95}]})
        self.assertEqual(clean["holdings"], [])                       # lower than current: refused
        clean, _ = self.run_pm({"holdings": [{"symbol": "HLD", "action": "tighten", "new_stop": 109.5}]})
        self.assertEqual(clean["holdings"], [])                       # too close to price: refused
        clean, _ = self.run_pm({"holdings": [{"symbol": "HLD", "action": "tighten", "new_stop": 105}]})
        self.assertEqual(clean["holdings"][0]["new_stop"], 105)

    def test_legacy_exit_untouchable_and_capacity(self):
        clean, _ = self.run_pm({"holdings": [{"symbol": "OLD", "action": "hold"}]})
        self.assertEqual(clean["holdings"], [])
        clean, _ = self.run_pm({"entries": [{"symbol": "AAA"}, {"symbol": "BBB"}]}, capacity=1)
        self.assertEqual(len(clean["entries"]), 2)                    # capacity + backups
        clean, _ = self.run_pm({"entries": [{"symbol": "AAA"}, {"symbol": "BBB"}]}, capacity=0)
        self.assertEqual(len(clean["entries"]), 0)                    # no room, no entries


class Tuning(unittest.TestCase):
    def test_bounds_and_risk_only_down(self):
        from engine import db, tuning
        db.init()
        with self.assertRaises(ValueError):
            tuning.validate("RISK_PER_TRADE", 0.03)                   # above owner's 1.5%
        with self.assertRaises(ValueError):
            tuning.validate("MAX_OPEN_RISK", 0.1)                     # not tunable at all
        with self.assertRaises(ValueError):
            tuning.validate("MOMENTUM.vol_mult", 9)
        old, new = tuning.apply("MOMENTUM.vol_mult", 1.8)
        self.assertEqual(R.MOMENTUM["vol_mult"], 1.8)
        tuning.apply("SETUPS_ENABLED.bounce", "off")
        self.assertFalse(R.SETUPS_ENABLED["bounce"])
        db.set_state("overrides", {})
        tuning.load()
        self.assertEqual(R.MOMENTUM["vol_mult"], 1.5)
        self.assertTrue(R.SETUPS_ENABLED["bounce"])


class Legacy(unittest.TestCase):
    def test_trend_stop_under_ma50(self):
        # price 100, 50d avg 95 -> stop 93.1 (2% under), tighter than 2.5 ATR (atr 4 -> 90)
        self.assertEqual(risk.legacy_stop(100, 4, 95, 80), 93.1)
        # 50d avg too close (99): 2% under = 97.02 is < 3% away -> fall back to ATR stop
        self.assertEqual(risk.legacy_stop(100, 4, 99, 80), 90.0)
        # below 50d but above 200d -> anchor on 200d
        self.assertEqual(risk.legacy_stop(100, 4, 110, 94), 92.12)

    def test_allocation_keeps_best_and_trims(self):
        keep = [dict(symbol="A", price=150, stop=140, qty=10, conviction=4),
                dict(symbol="B", price=120, stop=112, qty=10, conviction=3),
                dict(symbol="C", price=90, stop=84, qty=12, conviction=3),
                dict(symbol="D", price=40, stop=30, qty=50, conviction=1)]
        alloc, dropped = risk.allocate_legacy(keep, 15_000)
        self.assertNotIn("A", dropped)                       # best conviction survives
        for s, q in alloc.items():
            k = next(x for x in keep if x["symbol"] == s)
            self.assertLessEqual(q, k["qty"])                 # never adds shares
            self.assertGreaterEqual(q * k["price"], R.MIN_POSITION_USD)
        heat = sum(q * (next(x for x in keep if x["symbol"] == s)["price"] -
                        next(x for x in keep if x["symbol"] == s)["stop"]) for s, q in alloc.items())
        self.assertLessEqual(heat, R.LEGACY["heat_budget"] * 15_000 + 1)

    def test_owner_full_keep_comes_first(self):
        keep = [dict(symbol="HOLDA", price=94.23, stop=80.42, qty=18, conviction=5, full=True),
                dict(symbol="HOLDB", price=123.25, stop=107.02, qty=10, conviction=5, full=True),
                dict(symbol="ORCL", price=148.7, stop=138.34, qty=10, conviction=3),
                dict(symbol="AAOI", price=110.6, stop=103.38, qty=8, conviction=3),
                dict(symbol="STM", price=51.82, stop=47.82, qty=13, conviction=2),
                dict(symbol="HOLDC", price=92.0, stop=87.01, qty=12, conviction=4)]
        alloc, dropped = risk.allocate_legacy(keep, 15_856)
        self.assertEqual(alloc["HOLDA"], 18)
        self.assertEqual(alloc["HOLDB"], 10)
        self.assertEqual(dropped, [])
        for s in ("ORCL", "AAOI", "STM", "HOLDC"):
            k = next(x for x in keep if x["symbol"] == s)
            self.assertLess(alloc[s], k["qty"])                         # others trimmed
        heat = sum(q * (next(x for x in keep if x["symbol"] == s)["price"] -
                        next(x for x in keep if x["symbol"] == s)["stop"]) for s, q in alloc.items())
        self.assertLessEqual(heat, R.LEGACY["heat_budget"] * 15_856 + 1)
        print("\n   plan:", alloc, f"heat {heat / 15_856:.2%}")


class OwnerKept(unittest.TestCase):
    def test_owner_kept_only_stop_can_sell(self):
        from engine.analyst import validate_pm
        pos = dict(symbol="HOLDD", setup="legacy", entry_price=17.8, risk_per_share=1.6, stop=16.2, qty=50,
                   partial_done=0, entry_date=str(date.today() - timedelta(days=60)), max_exit_date=None, legacy=2)
        acts = risk.exit_signals(pos, 17.9, {}, date.today(), 15_000, date.today() + timedelta(days=1))
        self.assertFalse([a for a in acts if a[0] == "exit"])            # no earnings / time exit
        clean, dropped = validate_pm({"holdings": [{"symbol": "HOLDD", "action": "exit"}]}, [],
                                     [dict(symbol="HOLDD", setup="legacy", price=17.9, stop=16.2, owner_kept=True)],
                                     {"HOLDD": 17.9}, 1)
        self.assertEqual(clean["holdings"], [])


class FakeAPIError(Exception):
    """Shaped like anthropic.APIStatusError: status_code + body.error.message."""
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code, self.message = status, msg
        self.body = {"type": "error", "error": {"type": "x", "message": msg}}


class KeyMonitor(unittest.TestCase):
    def setUp(self):
        from engine import analyst, db, notify
        db.init()
        db.set_state("ai_key", None)
        db.set_state("ai_key_fail", None)
        self.sent = []
        self._send, notify.send = notify.send, lambda text, key=None, every=0, critical=False: \
            self.sent.append((text, critical))
        self.a, self.clock = analyst, [1_000_000.0]
        self._time, analyst.time.time = analyst.time.time, lambda: self.clock[0]

    def tearDown(self):
        from engine import notify
        notify.send = self._send
        self.a.time.time = self._time

    def test_classify(self):
        c = self.a.classify_error
        self.assertEqual(c(FakeAPIError(400, "Your credit balance is too low to access the Anthropic API. "
                                             "Please go to Plans & Billing to upgrade or purchase credits.")), "credit")
        self.assertEqual(c(FakeAPIError(400, "You have reached your specified API usage limits.")), "credit")
        self.assertEqual(c(FakeAPIError(401, "invalid x-api-key")), "auth")
        self.assertEqual(c(FakeAPIError(403, "Your API key does not have permission")), "auth")
        self.assertEqual(c(FakeAPIError(400, "This organization has been disabled.")), "auth")
        self.assertEqual(c(FakeAPIError(404, "model: claude-sonnet-4-6")), "model")
        self.assertIsNone(c(FakeAPIError(429, "Number of request tokens has exceeded your per-minute rate limit")))
        self.assertIsNone(c(FakeAPIError(529, "Overloaded")))
        self.assertIsNone(c(FakeAPIError(500, "Internal server error")))
        self.assertIsNone(c(FakeAPIError(400, "prompt is too long: 250000 tokens > 200000 maximum")))
        self.assertIsNone(c(Exception("Connection error.")))
        self.assertEqual(c(Exception("The api_key client option must be set")), "auth")

    def test_alert_once_a_day_then_recovery(self):
        a = self.a
        a.mark_key(True)
        self.assertEqual(self.sent, [])                                   # healthy: silent
        a.mark_key(False, "credit", "credit balance is too low")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1] and "stopped working" in self.sent[0][0])
        self.assertIn("secrets_local.py", self.sent[0][0])
        self.clock[0] += 3600
        a.mark_key(False, "credit", "credit balance is too low")
        self.assertEqual(len(self.sent), 1)                               # no spam within a day
        self.assertTrue(a.key_suspect())
        self.clock[0] += 24 * 3600
        a.mark_key(False, "credit", "credit balance is too low")
        self.assertEqual(len(self.sent), 2)                               # daily reminder
        a.mark_key(True)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("working again", self.sent[-1][0])
        self.assertFalse(a.key_suspect())
        a.mark_key(True)
        self.assertEqual(len(self.sent), 3)

    def test_unexplained_failures_escalate_after_6h(self):
        a = self.a
        for _ in range(2):
            a.note_failure("Connection error.")
            self.clock[0] += 4 * 3600
        self.assertEqual(self.sent, [])
        self.assertTrue(a.key_suspect())                                  # re-checks every 30 min meanwhile
        a.note_failure("Connection error.")                               # 3rd try, 8h in
        self.assertEqual(len(self.sent), 1)
        self.assertIn("6+ hours", self.sent[0][0])
        a.mark_key(True)
        self.assertFalse(a.key_suspect())

    def test_transient_blip_clears(self):
        a = self.a
        a.note_failure("Overloaded")
        a.mark_key(True)
        self.assertFalse(a.key_suspect())
        self.assertEqual(self.sent, [])

    def test_call_and_ping_mark_the_key(self):
        a = self.a
        orig = a._client

        class Msgs:
            def __init__(s, err): s.err = err
            def create(s, **kw):
                if s.err:
                    raise s.err
                return type("R", (), {"content": [], "usage": None})()

        def client(err):
            return lambda: type("C", (), {"messages": Msgs(err)})()
        try:
            a._client = client(FakeAPIError(401, "invalid x-api-key"))
            self.assertIs(a.key_health(), False)
            self.assertEqual(a.key_status()["kind"], "auth")
            a._client = client(FakeAPIError(529, "Overloaded"))
            self.assertIsNone(a.key_health())                             # can't tell: state unchanged
            self.assertFalse(a.key_status()["ok"])
            a._client = client(None)
            self.assertIs(a.key_health(), True)
            self.assertTrue(a.key_status()["ok"])
            a._client = client(FakeAPIError(400, "Your credit balance is too low"))
            with self.assertRaises(FakeAPIError):
                a._call("role", {}, 10, with_strategy=False)
            self.assertEqual(a.key_status()["kind"], "credit")
        finally:
            a._client = orig

    def test_run_loop_schedule(self):
        from datetime import datetime
        from engine import data, db, run
        calls = []
        orig = self.a.key_health
        self.a.key_health = lambda: calls.append(1)
        otime = run.time.time
        t = [5_000_000.0]
        run.time.time = lambda: t[0]
        try:
            db.set_state("done_keycheck", None)
            day = datetime(2026, 9, 23, 7, 0, tzinfo=data.ET)
            run.key_check(day)
            self.assertEqual(calls, [])                                   # before 8:30, healthy
            run.key_check(day.replace(hour=8, minute=31))
            run.key_check(day.replace(hour=12))
            self.assertEqual(len(calls), 1)                               # once a day
            db.set_state("ai_key", dict(ok=False, kind="credit"))
            t[0] += 600
            run.key_check(day.replace(hour=12, minute=10))
            self.assertEqual(len(calls), 1)
            t[0] += 1800
            run.key_check(day.replace(hour=12, minute=40))
            self.assertEqual(len(calls), 2)                               # every 30 min while down
        finally:
            self.a.key_health = orig
            run.time.time = otime


class Metrics(unittest.TestCase):
    """The scorekeeping must be honest: same replay for taken, vetoed and passed."""
    def setUp(self):
        from engine import db
        db.init()
        with db.conn() as c:
            c.execute("DELETE FROM shadow")
            c.execute("DELETE FROM trades")

    def test_attribution_needs_both_groups_before_judging(self):
        from engine import db, metrics
        for i in range(metrics.MIN_FOR_VERDICT + 5):
            db.save_shadow(f"2026-01-{i % 28 + 1:02d}", f"T{i}", "momentum",
                           "taken" if i % 2 else "passed", i, 100.0, 10.0, 9.0)
            db.score_shadow(f"2026-01-{i % 28 + 1:02d}", f"T{i}",
                            1.0 if i % 2 else -1.0, 5, "stop")
        at = metrics.attribution()
        self.assertGreater(at["groups"]["taken"]["traded"], 0)
        self.assertGreater(at["groups"]["passed"]["traded"], 0)
        # with too few in one group it must refuse to call it
        with db.conn() as c:
            c.execute("DELETE FROM shadow WHERE decision='passed'")
        self.assertEqual(metrics.attribution()["verdict"], "not enough data yet")
        self.assertIsNone(metrics.attribution()["edge_r"])

    def test_attribution_calls_a_loss_a_loss(self):
        from engine import db, metrics
        n = metrics.MIN_FOR_VERDICT
        for i in range(n * 2):
            d = f"2026-02-{i % 28 + 1:02d}"
            taken = i < n
            db.save_shadow(d, f"S{i}", "bounce", "taken" if taken else "vetoed", i, 1.0, 10.0, 9.0)
            db.score_shadow(d, f"S{i}", -0.5 if taken else 1.5, 4, "stop")
        at = metrics.attribution()
        self.assertLess(at["edge_r"], 0)
        self.assertIn("worse", at["verdict"])           # no flattering the AI

    def test_scorecard_expectancy(self):
        from engine import db, metrics
        db.record_trade("A", "bounce", "2026-01-01", "2026-01-05", 100, 110, 10, 5, "target")
        db.record_trade("B", "bounce", "2026-01-02", "2026-01-06", 100, 95, 10, 5, "stop")
        sc = metrics.scorecard()
        self.assertEqual(sc["all"]["n"], 2)
        self.assertAlmostEqual(sc["all"]["expectancy"], 0.5, places=3)   # (+2R, -1R) / 2
        self.assertEqual(sc["all"]["win_pct"], 50.0)
        self.assertFalse(sc["enough_data"])

    def test_benchmark_compares_like_for_like(self):
        from engine import db, metrics
        db.save_bars("SPY", [("2026-03-02", 100, 101, 99, 100.0, 1e6),
                             ("2026-03-03", 100, 101, 99, 110.0, 1e6)])
        b = metrics.benchmark([["2026-03-02", 10_000.0], ["2026-03-03", 10_500.0]])
        self.assertEqual(b["me_pct"], 5.0)
        self.assertEqual(b["spy_pct"], 10.0)
        self.assertEqual(b["diff_pct"], -5.0)           # behind the index, and says so


class Execution(unittest.TestCase):
    def setUp(self):
        from engine import db
        db.init()
        db.set_state("slippage", None)

    def test_slippage_sign_is_honest(self):
        from engine import core, db, metrics
        # paid 57.40 for a 57.22 trigger: 31 bps worse for us, so negative
        core._record_slip("entry", "CDNA", 57.22, 57.40, "2026-09-24")
        # stop at 54.10 filled at 53.20 on a gap: 166 bps below, also negative
        core._record_slip("stop", "CDNA", 54.10, 53.20, "2026-09-25")
        e = metrics.execution()
        self.assertLess(e["entry"]["avg_bps"], 0)
        self.assertAlmostEqual(e["entry"]["avg_bps"], -31.5, places=0)
        self.assertLess(e["stop"]["avg_bps"], 0)
        self.assertAlmostEqual(e["stop"]["avg_bps"], -166.4, places=0)
        note = metrics.execution_note()
        self.assertIn("worse", note)
        self.assertIn("below", note)

    def test_a_good_fill_reads_as_good(self):
        from engine import core, db, metrics
        core._record_slip("entry", "X", 100.0, 99.50, "2026-09-24")   # filled cheaper
        self.assertGreater(metrics.execution()["entry"]["avg_bps"], 0)
        self.assertIn("better", metrics.execution_note())

    def test_bad_input_never_raises(self):
        from engine import core
        for ref, fill in ((0, 10), (10, 0), (None, 5), ("x", 5)):
            core._record_slip("entry", "X", ref, fill, "2026-09-24")  # must not raise
