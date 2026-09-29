"""The four setups, as pure functions over an enriched daily DataFrame.

Each `scan_*` runs after the close and returns a candidate for tomorrow
(or None). A candidate says how to enter (`entry`), the trigger price, the
initial stop and a score used only for ranking.

entry types:
  breakout    buy intraday once price >= trigger with volume confirming
  near_close  re-check with the live price near the close, then buy (bounce)
"""
import math

from . import rules as R
from .indicators import rsi2_with_price


def _ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _stop(trigger, atr, atr_mult, max_pct):
    """ATR-based stop, but never wider than max_pct below the trigger."""
    return round(max(trigger - atr_mult * atr, trigger * (1 - max_pct)), 2)


def _valid(trigger, stop):
    return _ok(trigger) and _ok(stop) and stop < trigger and (trigger - stop) / trigger >= 0.01


def scan_momentum(sym, d, rs_pct):
    """Strong uptrend, top-20% relative strength, coiled just under its 20-day high."""
    p = R.MOMENTUM
    t = d.iloc[-1]
    if not all(_ok(t[k]) for k in ("ma50", "ma200", "atr", "high252")):
        return None
    if not (t.close > t.ma50 > t.ma200):
        return None
    if rs_pct is None or rs_pct < 0.80:
        return None
    if t.close < 0.85 * t.high252:
        return None
    level = float(d["high"].tail(p["lookback_high"]).max())
    if t.close < 0.95 * level:           # not close enough to a breakout
        return None
    trigger = round(level * 1.001, 2)
    stop = _stop(trigger, t.atr, p["stop_atr"], p["max_stop_pct"])
    if not _valid(trigger, stop):
        return None
    score = rs_pct * 100 + (1 - (level - t.close) / level) * 10
    return dict(symbol=sym, setup="momentum", entry="breakout", trigger=trigger, stop=stop,
                score=round(score, 2), vol50=float(t.vol50), atr=float(t.atr),
                note=f"20d high {level:.2f}, RS pct {rs_pct:.0%}")


def scan_bounce(sym, d):
    """Pre-filter: uptrend pulling back. Final check happens near the close."""
    t = d.iloc[-1]
    if not all(_ok(t[k]) for k in ("ma50", "ma200", "atr", "high10")):
        return None
    if not (t.close > t.ma200 and t.ma50 > t.ma200):
        return None
    drop = (t.high10 - t.close) / t.high10
    if drop < 0.05:
        return None
    stop = _stop(t.close, t.atr, R.BOUNCE["stop_atr"], R.BOUNCE["max_stop_pct"])
    return dict(symbol=sym, setup="bounce", entry="near_close", trigger=round(float(t.close), 2),
                stop=stop, score=round(drop * 100, 2), atr=float(t.atr),
                high10=float(t.high10), ma200=float(t.ma200),
                note=f"down {drop:.0%} from 10d high")


def bounce_confirm(d, price):
    """Near-close check with the live price. Returns (ok, stop, reason)."""
    p = R.BOUNCE
    t = d.iloc[-1]
    high10 = max(float(t.high10), price)
    drop = (high10 - price) / high10
    r2 = rsi2_with_price(d["close"], price)
    if price <= t.ma200:
        return False, None, "lost the 200-day average"
    if drop < p["drop_from_10d_high"]:
        return False, None, f"only down {drop:.1%} from 10d high"
    if r2 > p["rsi2_max"]:
        return False, None, f"RSI(2) {r2:.0f} not oversold"
    stop = _stop(price, t.atr, p["stop_atr"], p["max_stop_pct"])
    return True, stop, f"down {drop:.1%}, RSI(2) {r2:.0f}"


def scan_catalyst(sym, d, earnings_dates):
    """Post-earnings gap-up that held. Never enters before a report."""
    p = R.CATALYST
    if len(d) < 60:
        return None
    for back in (1, 2, 3):                       # gap day = today, yesterday or the day before
        if len(d) < back + 1:
            break
        g = d.iloc[-back]
        prev_close = d["close"].iloc[-back - 1]
        if not _ok(g.vol50) or g.vol50 <= 0:
            continue
        gap = g.open / prev_close - 1
        if gap < p["min_gap"] or g.volume < p["vol_mult"] * g.vol50:
            continue
        if g.close < (g.high + g.low) / 2:        # faded the gap
            continue
        gday = d.index[-back].date()
        if not any(0 <= (gday - e).days <= p["earnings_window_days"] for e in earnings_dates):
            continue
        since = d.iloc[-back:]
        if since["low"].min() < g.low:            # broke the gap-day low: failed
            return None
        trigger = round(float(since["high"].max()) * 1.001, 2)
        stop = round(max(float(g.low) * 0.995, trigger * (1 - p["max_stop_pct"])), 2)
        if not _valid(trigger, stop):
            return None
        return dict(symbol=sym, setup="catalyst", entry="breakout", trigger=trigger, stop=stop,
                    score=round(gap * 100 + g.volume / g.vol50, 2), vol50=float(d.iloc[-1].vol50),
                    atr=float(d.iloc[-1].atr), gap_date=str(gday),
                    note=f"earnings gap +{gap:.0%} on {g.volume / g.vol50:.1f}x volume")
    return None


def value_fundamentals_ok(f, sector_pe):
    """Cheap vs its sector, growing, not over-levered -- and not a cyclical at its peak.

    The extra tests below only run when the profile asks for them (the CashMoney rulebook
    sets them; Luck leaves them out and behaves exactly as before). They exist because a
    bare P/E screen reliably finds commodity producers at the top of their earnings cycle,
    where the low multiple is a symptom rather than an opportunity.
    """
    p = R.VALUE
    pe, rev, eps, de = (f.get("peRatio"), f.get("revChangeTTM"),
                        f.get("epsChangePercentTTM"), f.get("totalDebtToEquity"))
    if not (_ok(pe) and pe > 0 and sector_pe and pe <= sector_pe * p["max_pe_vs_sector"]):
        return False
    if not (_ok(rev) and rev >= p["min_rev_growth"]):
        return False
    if not (_ok(eps) and eps > 0):
        return False
    if _ok(de) and de > p["max_debt_to_equity"]:
        return False

    floor = p.get("min_pe_vs_sector")
    if floor and pe < sector_pe * floor:
        return False                          # priced for an earnings collapse
    cap = p.get("max_eps_growth")
    if cap and eps > cap:
        return False                          # a cycle peaking, not a business compounding
    roll = p.get("min_margin_mrq_vs_ttm")
    if roll:
        ttm, mrq = f.get("netProfitMarginTTM"), f.get("netProfitMarginMRQ")
        if _ok(ttm) and _ok(mrq) and ttm > 0 and mrq < ttm * roll:
            return False                      # the peak has already started rolling over
    roe_min = p.get("min_roe")
    if roe_min:
        roe = f.get("returnOnEquity")
        if _ok(roe) and roe < roe_min:
            return False                      # only judged when the figure is there
    cr_min = p.get("min_current_ratio")
    if cr_min:
        cr = f.get("currentRatio")
        if _ok(cr) and cr > 0 and cr < cr_min:
            return False                      # absent for banks, so only checked when present
    return True


def scan_value(sym, d, f, sector_pe):
    """Cheap, growing company whose price just reclaimed its 50-day average.

    In the CashMoney profile (VALUE["mode"] == "trend") the entry test is different:
    an established uptrend rather than a fresh reclaim. See _scan_value_trend.
    """
    p = R.VALUE
    if not value_fundamentals_ok(f, sector_pe):
        return None
    if p.get("mode") == "trend":
        return _scan_value_trend(sym, d, f, sector_pe)
    t = d.iloc[-1]
    if not all(_ok(t[k]) for k in ("ma50", "ma200", "atr")):
        return None
    if t.close <= t.ma50 or t.close < 0.9 * t.ma200:
        return None
    recent = d.tail(p["reclaim_days"] + 1).iloc[:-1]
    if not (recent["close"] < recent["ma50"]).any():   # must be a fresh reclaim
        return None
    trigger = round(float(t.high) * 1.001, 2)
    stop = _stop(trigger, t.atr, p["stop_atr"], p["max_stop_pct"])
    if not _valid(trigger, stop):
        return None
    discount = 1 - f["peRatio"] / sector_pe
    return dict(symbol=sym, setup="value", entry="breakout", trigger=trigger, stop=stop,
                score=round(discount * 50 + min(f.get("revChangeTTM", 0), 50), 2),
                vol50=float(t.vol50), atr=float(t.atr),
                note=f"P/E {f['peRatio']:.1f} vs sector {sector_pe:.1f}, rev {f.get('revChangeTTM', 0):+.0f}%")


def _scan_value_trend(sym, d, f, sector_pe):
    """CashMoney's only setup: cheap, growing, and already turning up.

    The trend test is the point. A cheap stock below a falling 200-day average is
    not a bargain this account is allowed to catch, however good the story -- that
    is where value investing goes to die, and it is the one rule with no exceptions.
    """
    p = R.VALUE
    t = d.iloc[-1]
    if not all(_ok(t[k]) for k in ("ma50", "ma200", "atr")):
        return None
    if t.close <= t.ma200:                       # must be above the line
        return None
    slope_days = p.get("ma200_slope_days", 21)
    if len(d) <= slope_days:
        return None
    then = d["ma200"].iloc[-1 - slope_days]
    if not _ok(then) or t.ma200 < then:          # ...and the line must be flat or rising
        return None
    if t.close > t.ma200 * (1 + p.get("max_extension", 0.30)):
        return None                              # this far above trend is momentum, not value
    trigger = round(float(t.high) * 1.001, 2)
    stop = _stop(trigger, t.atr, p["stop_atr"], p["max_stop_pct"])
    if not _valid(trigger, stop):
        return None
    discount = 1 - f["peRatio"] / sector_pe
    above = t.close / t.ma200 - 1
    return dict(symbol=sym, setup="value", entry="breakout", trigger=trigger, stop=stop,
                score=round(discount * 60 + min(f.get("revChangeTTM", 0), 40)
                            - above * 20, 2),
                vol50=float(t.vol50), atr=float(t.atr),
                note=(f"P/E {f['peRatio']:.1f} vs sector {sector_pe:.1f}, "
                      f"rev {f.get('revChangeTTM', 0):+.0f}%, "
                      f"{above * 100:.0f}% above a rising 200-day"))
