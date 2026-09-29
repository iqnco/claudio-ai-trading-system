"""The CashMoney core: an index position held on a monthly trend rule.

Half of this account is meant to be the market, not a stock picker's opinion of it.
It is held while the index is in an uptrend and sold when the trend breaks, judged
once a month on a monthly closing price. Checking daily is what ruins this rule --
the whipsaws eat the benefit -- so the decision is deliberately slow and the
re-entry needs the index 2% clear of the line, not merely back over it.

Everything here is a pure function of bars and numbers. Nothing places an order;
core_plan() returns what should happen and the caller decides whether to do it.
"""
import math

from . import rules as R


def _month(ts):
    return (ts.year, ts.month)


def last_completed_month(d, today):
    """(close, ma, month_key, date) for the final bar of the last finished month.

    None when there isn't one yet, or when the average isn't computable.
    """
    if d is None or len(d) == 0 or R.CORE is None:
        return None
    this = _month(today)
    ma_col = f"ma{R.CORE['trend_ma']}"
    if ma_col not in d.columns:
        return None
    prior = [ts for ts in d.index if _month(ts) != this and _month(ts) < this]
    if not prior:
        return None
    ts = prior[-1]
    row = d.loc[ts]
    close, ma = float(row["close"]), float(row[ma_col])
    if math.isnan(ma):
        return None
    return dict(close=close, ma=ma, month=_month(ts), date=str(ts.date()))


def wants_in(close, ma, currently_in, buffer=None):
    """The trend rule, with its asymmetry: easy to stay, harder to come back."""
    buf = R.CORE["reentry_buffer"] if buffer is None else buffer
    return close >= ma if currently_in else close >= ma * (1 + buf)


def core_plan(d, today, held_qty, price, equity, last_month_done=None):
    """What the core should do this month.

    Returns dict(action, qty, reason, month, close, ma). action is one of
    'hold', 'buy', 'sell', 'trim', 'wait'. equity is MANAGED equity -- the owner's
    own holdings are not part of it and never will be.
    """
    C = R.CORE
    if C is None:
        return dict(action="hold", qty=0, reason="no core in this profile", month=None)
    m = last_completed_month(d, today)
    if not m:
        return dict(action="wait", qty=0, reason="not enough history for the trend rule",
                    month=None)
    out = dict(month=m["month"], close=round(m["close"], 2), ma=round(m["ma"], 2),
               as_of=m["date"])
    if last_month_done == list(m["month"]) or last_month_done == m["month"]:
        return dict(out, action="hold", qty=0, reason="this month's check is already done")
    if not price or price <= 0 or not equity or equity <= 0:
        return dict(out, action="wait", qty=0, reason="no price or no equity")

    invested = held_qty > 0
    want = wants_in(m["close"], m["ma"], invested)
    pct = m["close"] / m["ma"] - 1
    where = f"{C['symbol']} closed {m['date']} at ${m['close']:,.2f}, {pct:+.1%} vs its {C['trend_ma']}-day average"

    if not want:
        if invested:
            return dict(out, action="sell", qty=int(held_qty),
                        reason=f"{where}: trend broken, core goes to cash")
        return dict(out, action="hold", qty=0,
                    reason=f"{where}: still below the line, core stays in cash")

    target_usd = equity * C["target_pct"]
    target_qty = int(target_usd // price)
    if not invested:
        if target_qty < 1 or target_qty * price < C["min_trade_usd"]:
            return dict(out, action="wait", qty=0, reason="core stake would be too small to bother")
        return dict(out, action="buy", qty=target_qty,
                    reason=f"{where}: uptrend, buy the core back to {C['target_pct']:.0%}")

    drift = (held_qty * price - target_usd) / target_usd if target_usd else 0.0
    if abs(drift) <= C["rebalance_band"]:
        return dict(out, action="hold", qty=0,
                    reason=f"{where}: uptrend, core is {drift:+.0%} off target -- leave it alone")
    diff = abs(target_qty - int(held_qty))
    if diff < 1 or diff * price < C["min_trade_usd"]:
        return dict(out, action="hold", qty=0, reason="rebalance too small to be worth a trade")
    if drift > 0:
        return dict(out, action="trim", qty=diff,
                    reason=f"{where}: core drifted {drift:+.0%} above target, sell {diff}")
    return dict(out, action="buy", qty=diff,
                reason=f"{where}: core drifted {drift:+.0%} below target, buy {diff}")


def core_cash_reserve(action, qty, price, held_qty, equity):
    """Cash the stock picker may not spend, in dollars.

    Three states, and only one of them reserves anything:
      invested          -> the core's money is in the core; nothing to hold back
      buying this month -> hold back exactly what the purchase costs
      sitting out       -> hold back the whole target. A trend break raises a large pile
                           of cash at a market low, which is precisely when the stock
                           picker must not be handed it.
    """
    if not R.CORE:
        return 0.0
    if action == "buy":
        return float(qty or 0) * float(price or 0.0)
    if (held_qty or 0) > 0:
        return 0.0
    return float(equity or 0.0) * R.CORE["target_pct"]


def trend_break(d, today):
    """True when a satellite's last monthly close is below its trend average.

    This sells a position the stop has not reached yet: the reason for owning it was
    'cheap and turning up', and half of that has stopped being true.
    """
    if R.TREND_BREAK_MA is None or d is None or len(d) == 0:
        return None
    col = f"ma{R.TREND_BREAK_MA}"
    if col not in d.columns:
        return None
    this = _month(today)
    prior = [ts for ts in d.index if _month(ts) < this]
    if not prior:
        return None
    row = d.loc[prior[-1]]
    ma = float(row[col])
    if math.isnan(ma):
        return None
    return float(row["close"]) < ma


def idle_cash_note(cash, annual_yield=0.0367):
    """What the un-deployed cash is costing by sitting in the sweep.

    Moving it into a money market fund is the owner's action, never an automated one.
    """
    if not cash or cash < 1000:
        return ""
    return (f"Idle cash ${cash:,.0f} is earning close to nothing in the sweep; at "
            f"{annual_yield:.2%} in a money market fund that is about "
            f"${cash * annual_yield:,.0f} a year, or ${cash * annual_yield / 12:,.0f} a month.")
