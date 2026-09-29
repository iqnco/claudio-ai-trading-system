"""Risk engine: pure functions, no I/O. Every entry passes through size_trade().

The AI analyst can shrink a trade (size_mult 0.5) or veto it. Nothing here
can be overridden by it: all limits come from rules.py.
"""
import math
from dataclasses import dataclass, field

from . import rules as R


@dataclass
class Holding:
    symbol: str
    qty: float
    price: float
    stop: float | None
    sector: str = ""
    market_cap: float | None = None
    setup: str = ""

    @property
    def value(self):
        return self.qty * self.price

    @property
    def open_risk(self):
        if self.setup == "core":         # the index core is governed by the monthly trend
            return 0.0                   # rule, not a stop; it is not part of the heat
        if self.stop is None:            # unprotected: whole position counts as risk
            return self.value
        return max(0.0, (self.price - self.stop) * self.qty)

    @property
    def small(self):
        return self.market_cap is not None and self.market_cap < R.SMALLCAP_MAX_MCAP


@dataclass
class Snapshot:
    equity: float
    settled_cash: float
    holdings: list = field(default_factory=list)
    reserved_cash: float = 0.0          # tied up in working buy orders
    pending_entries: int = 0            # working buy orders not yet filled
    entries_today: int = 0
    entries_this_month: int = 0
    regime: str = "full"
    halt: str = "ok"                    # ok | day_halt | full_halt | manual

    def open_risk(self):
        return sum(h.open_risk for h in self.holdings)

    def sector_value(self, sector):
        return sum(h.value for h in self.holdings if h.sector == sector)

    def small_value(self):
        return sum(h.value for h in self.holdings if h.small)

    def count(self, setup=None):
        return sum(1 for h in self.holdings if setup is None or h.setup == setup)


def regime(spy_close, spy_ma50, spy_ma200, vix):
    if spy_close < spy_ma200 or (vix is not None and vix > R.VIX_RISK_OFF):
        return "risk_off"
    if spy_close < spy_ma50:
        return "caution"
    return "full"


def regime_mult(reg, setup):
    """Risk multiplier for a setup under a regime. 0 = not allowed."""
    if reg == "risk_off":
        return R.CAUTION_RISK_MULT if setup == "bounce" else 0.0
    if reg == "caution":
        return R.CAUTION_RISK_MULT
    return 1.0


def kill_switch(equity, day_start_equity, peak_equity):
    if peak_equity and equity <= peak_equity * (1 - R.TOTAL_DRAWDOWN_HALT):
        return "full_halt"
    if day_start_equity and equity <= day_start_equity * (1 - R.DAILY_LOSS_HALT):
        return "day_halt"
    return "ok"


def size_trade(snap, symbol, setup, entry, stop, sector, market_cap,
               size_mult=1.0, cooldown_active=False):
    """Returns (qty, reason). qty == 0 means rejected, reason says why."""
    if snap.halt != "ok":
        return 0, f"halted ({snap.halt})"
    if any(h.symbol == symbol for h in snap.holdings):
        return 0, "already held"
    if cooldown_active:
        return 0, f"stopped out < {R.REENTRY_COOLDOWN_DAYS} days ago"
    if snap.entries_today >= R.MAX_NEW_ENTRIES_PER_DAY:
        return 0, "daily entry limit reached"
    if R.MAX_NEW_ENTRIES_PER_MONTH and snap.entries_this_month >= R.MAX_NEW_ENTRIES_PER_MONTH:
        return 0, f"monthly entry limit reached ({R.MAX_NEW_ENTRIES_PER_MONTH})"
    if snap.count() - snap.count("core") + snap.pending_entries >= R.MAX_POSITIONS:
        return 0, "max positions"
    if setup == "value" and snap.count("value") >= R.MAX_VALUE_POSITIONS:
        return 0, "max value positions"
    if stop is None or entry <= 0 or stop >= entry:
        return 0, "invalid stop"

    mult = (regime_mult(snap.regime, setup) * max(0.0, min(1.0, size_mult))
            * float(R.SETUP_SIZE_MULT.get(setup, 1.0)))
    if mult == 0:
        return 0, f"{setup} not allowed in {snap.regime} regime"

    eq = snap.equity
    budget = eq * R.RISK_PER_TRADE * mult
    heat_room = eq * R.MAX_OPEN_RISK - snap.open_risk()
    if heat_room < budget * 0.5:
        return 0, f"open-risk budget full ({snap.open_risk() / eq:.1%} of {R.MAX_OPEN_RISK:.1%})"
    risk_dollars = min(budget, heat_room)
    per_share = entry - stop
    qty = math.floor(risk_dollars / per_share)

    small = market_cap is not None and market_cap < R.SMALLCAP_MAX_MCAP
    cap_pct = R.MAX_POSITION_PCT_SMALLCAP if small else R.MAX_POSITION_PCT
    limits = {
        "position cap": eq * cap_pct,
        "sector cap": eq * R.MAX_SECTOR_PCT - snap.sector_value(sector),
        "settled cash": snap.settled_cash - snap.reserved_cash - eq * R.MIN_CASH_PCT,
    }
    if small:
        limits["small-cap cap"] = eq * R.MAX_SMALLCAP_TOTAL - snap.small_value()
    binding = None
    for name, dollars in limits.items():
        allowed = math.floor(max(0.0, dollars) / entry)
        if allowed < qty:
            qty, binding = allowed, name

    if qty < 1 or qty * entry < R.MIN_POSITION_USD:
        return 0, f"too small after {binding or 'risk'} limit (${qty * entry:,.0f})"
    note = f"risk ${qty * per_share:,.0f} ({qty * per_share / eq:.2%})"
    if binding:
        note += f", capped by {binding}"
    return qty, note


def exit_signals(pos, price, bar_ctx, today, equity, next_earnings=None):
    """Decide what to do with an open position. Pure: returns a list of actions.

    pos: dict from db.positions. bar_ctx: dict with low10, low20, ma5, ma50, close from
    the latest completed daily bar. Actions: ('raise_stop', px), ('partial', qty),
    ('earnings_trim', qty), ('exit', reason).
    """
    acts = []
    entry, rps, setup = pos["entry_price"], pos["risk_per_share"], pos["setup"]
    stop = pos["stop"]
    r_now = (price - entry) / rps if rps else 0.0
    # Ratchets that tighten the stop read the last completed close, never the live price:
    # an intraday wick to +1R must not move the stop to breakeven and hand the trade back.
    # r_close is None until there is a completed bar (the entry day), which disables them.
    c = bar_ctx.get("close")
    r_close = (c - entry) / rps if (c and rps) else None
    # A trade that has closed at +3R is a runner: no time limits, slower trail. high_water
    # is the best close since entry, so a runner never reverts to swing-trade rules.
    hw = pos.get("high_water") or entry
    runner = rps > 0 and (hw - entry) / rps >= R.RUNNER_AT_R

    owner_kept = pos.get("legacy") == 2
    # time stop / max hold (a runner has neither: it exits on the trail)
    if pos.get("max_exit_date") and str(today) >= pos["max_exit_date"] and not runner:
        if not (setup == "legacy" and r_now >= 1.0):
            return [("exit", "max hold reached")]
    tsd = {"momentum": R.MOMENTUM, "catalyst": R.CATALYST}.get(setup, {}).get("time_stop_days")
    if tsd and pos.get("entry_date") and not runner:
        held = _trading_days_between(pos["entry_date"], today)
        if held >= tsd and r_now < 1.0:
            return [("exit", f"time stop: {held} days without +1R")]
    # earnings: short-term setups don't hold through a report, unless the trade has a real
    # cushion and a stop at or above entry -- then half is sold and the rest runs through it.
    if (next_earnings is not None and not owner_kept and not R.HOLD_THROUGH_EARNINGS
            and 0 <= (next_earnings - today).days <= 1):
        cushion = (r_close if r_close is not None else r_now) >= R.EARNINGS_HOLD_MIN_R
        if setup in R.EARNINGS_HOLD_SETUPS and cushion and stop >= entry and not pos.get("earnings_trimmed"):
            half = math.floor(pos["qty"] * R.EARNINGS_HOLD_FRACTION)
            if half >= 1:
                return [("earnings_trim", half)]
        if setup in R.EXIT_BEFORE_EARNINGS:
            return [("exit", f"earnings on {next_earnings}")]
        if setup in ("value", "catalyst", "legacy"):
            small_enough = pos["qty"] * price <= R.VALUE_EARNINGS_MAX_PCT * equity
            if not (r_now >= 1.0 and small_enough):
                return [("exit", f"earnings on {next_earnings}, not enough cushion")]
    # bounce: take the snap-back
    if setup == "bounce":
        if bar_ctx.get("ma5") and price > bar_ctx["ma5"]:
            return [("exit", "closed back above 5-day average")]
        if r_now >= R.BOUNCE["target_r"]:
            return [("exit", f"+{r_now:.1f}R target")]

    new_stop = stop
    if r_close is not None and r_close >= R.BREAKEVEN_AT_R:
        new_stop = max(new_stop, entry)
    if r_now >= R.PARTIAL_AT_R and not pos.get("partial_done") and setup != "bounce":
        part = math.floor(pos["qty"] * R.PARTIAL_FRACTION)
        if part >= 1:
            acts.append(("partial", part))
    if setup in ("momentum", "catalyst"):
        trail = bar_ctx.get("low20") if runner else bar_ctx.get("low10")
        if trail:
            new_stop = max(new_stop, trail)
    if (setup in ("value", "legacy") or runner) and (r_close or 0) >= 1.0:
        ma_n = R.VALUE["trail_ma"] if setup == "value" else (
            R.LEGACY["trail_ma"] if setup == "legacy" else R.RUNNER_TRAIL_MA)
        buf = R.VALUE.get("trail_ma_buffer", 0.01) if setup == "value" else 0.01
        ma = bar_ctx.get(f"ma{ma_n}")
        if ma:
            new_stop = max(new_stop, ma * (1 - buf))
    if new_stop > stop and new_stop < price:
        acts.append(("raise_stop", round(new_stop, 2)))
    return acts


def _trading_days_between(start_iso, today):
    """Weekdays from start (exclusive) to today (inclusive). Holidays ignored."""
    from datetime import date, timedelta
    d = date.fromisoformat(str(start_iso)[:10])
    n = 0
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def add_trading_days(start, n):
    from datetime import timedelta
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def legacy_stop(price, atr, ma50, ma200):
    """Stop for a kept legacy holding: under the nearest trend average below the price
    (tighter and more meaningful than a pure volatility stop), else a volatility stop."""
    L = R.LEGACY
    stop = max(price - L["stop_atr"] * atr, price * (1 - L["max_stop_pct"]))
    for ma in (ma50, ma200):
        if ma is not None and ma == ma and ma < price:
            anchor = ma * (1 - L["ma_buffer"])
            if anchor > stop and (price - anchor) / price >= L["min_stop_pct"]:
                stop = anchor
            break
    return round(stop, 2)


def allocate_legacy(keepers, equity):
    """Split the legacy risk budget across keepers.

    keepers: list of dicts with symbol, price, stop, qty (held), conviction, small (bool),
    full (bool: the owner said keep every share). Owner-kept holdings are kept whole and their
    risk comes off the budget first. The rest share what's left, best conviction first; a
    keeper that can't hold a meaningful stake is dropped (sold) and its budget goes to the rest.
    Returns ({symbol: keep_qty}, [dropped symbols]).
    """
    budget = R.LEGACY["heat_budget"] * equity
    alloc = {}
    for k in keepers:
        if k.get("full"):
            alloc[k["symbol"]] = int(k["qty"])
            budget -= k["qty"] * (k["price"] - k["stop"])
    ranked = sorted((k for k in keepers if not k.get("full")), key=lambda k: -k["conviction"])
    room = max(0, R.MAX_POSITIONS - 1 - len(alloc))
    pool, dropped = ranked[:room], [k["symbol"] for k in ranked[room:]]
    budget = max(0.0, budget)
    while pool:
        per = budget / len(pool)
        trial, failing = {}, []
        for k in pool:
            rps = k["price"] - k["stop"]
            cap = (R.MAX_POSITION_PCT_SMALLCAP if k.get("small") else R.MAX_POSITION_PCT) * equity
            q = int(min(k["qty"], per / rps, cap / k["price"], R.RISK_PER_TRADE * equity / rps))
            trial[k["symbol"]] = q
            if q < 1 or q * k["price"] < R.MIN_POSITION_USD:
                failing.append(k)
        if not failing:
            alloc.update(trial)
            return alloc, dropped
        worst = min(failing, key=lambda k: k["conviction"])
        pool.remove(worst)
        dropped.append(worst["symbol"])
    return alloc, dropped
