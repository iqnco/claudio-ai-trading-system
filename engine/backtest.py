"""Replay the scanners and exit rules over the cached bar history.

    venv312/bin/python -m engine.backtest [--variants] [--symbols N]

Read-only: touches no account, places no orders, makes no AI calls. It answers one
question — given the signals this engine actually produces, do its exit rules make
or lose money, and which rule changes survive?

Method: every signal is simulated as a standalone trade risking exactly 1R, so the
results are expectancy per trade in R, independent of position sizing and capacity.
Entries use the next day's prices (never the signal day's close), and stops are checked
against the day's low before anything else, so a stop-out is never mistaken for a win.

Honest limits, stated up front:
  * ~420 days of history: one market regime, mostly bullish. Not a cycle.
  * Value trades are NOT tested: the fundamentals in the database are today's snapshot,
    so backtesting them would be pure lookahead.
  * The earnings rules are NOT tested: no historical earnings dates for most names.
  * Survivorship: the universe is today's listed stocks, so names that were delisted
    are missing. That flatters every result equally.
  * No AI layer. This measures the mechanical system only.
"""
import argparse
import json
import math
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from . import db, indicators, risk, setups
from . import rules as R

SLIPPAGE = 0.0015          # each way, on top of the trigger: spread + market impact
VOL_CONFIRM = True         # live requires volume on the breakout day; mirror that here
REGIME = None              # per-day SPY regime; momentum/catalyst are blocked outside "full"
MAX_TRADE_DAYS = 120       # hard stop on the simulation, not a trading rule
UNITS = 300.0              # shares per simulated trade: partials need a divisible size


# ── candidate generation ─────────────────────────────────────────────────
def _frames(limit_symbols=None):
    uni = db.get_universe()
    syms = sorted(uni)
    if limit_symbols:
        syms = syms[:limit_symbols]
    out = {}
    for s in syms:
        d = db.load_bars(s, 600)
        if len(d) < R.MIN_HISTORY_BARS + 20:
            continue
        d = indicators.enrich(d)
        # same liquidity gate the live scan uses
        if not (d["close"].iloc[-1] >= R.MIN_PRICE and d["dollar_vol50"].iloc[-1] >= R.MIN_DOLLAR_VOLUME):
            continue
        out[s] = d
    return out


def spy_regime(frames):
    """Per-day market regime from SPY, exactly as the live engine computes it.
    (VIX history isn't stored, so this uses the moving averages only.)"""
    d = frames.get("SPY")
    if d is None:
        d = indicators.enrich(db.load_bars("SPY", 600))
    return {ts.date(): risk.regime(r.close, r.ma50, r.ma200, None) for ts, r in d.iterrows()}


def _rs_percentiles(frames):
    """Cross-sectional 63-day-return percentile per (date, symbol), as the live scan does."""
    ret = pd.DataFrame({s: d["ret63"] for s, d in frames.items()})
    return ret.rank(axis=1, pct=True)


def candidates(frames, rs, start_i):
    """Run the real scanners on every day that could possibly produce a signal."""
    found = defaultdict(list)          # date -> [candidate]
    for sym, d in frames.items():
        n = len(d)
        c, ma50, ma200 = d["close"], d["ma50"], d["ma200"]
        hi20 = d["high"].rolling(R.MOMENTUM["lookback_high"]).max()
        mom_ok = (c > ma50) & (ma50 > ma200) & (c >= 0.85 * d["high252"]) & (c >= 0.95 * hi20)
        drop = (d["high10"] - c) / d["high10"]
        bnc_ok = (c > ma200) & (ma50 > ma200) & (drop >= 0.05)
        rs_sym = rs[sym] if sym in rs else None
        for i in range(max(start_i, R.MIN_HISTORY_BARS), n - 1):
            day = d.index[i]
            sub = None
            if bool(mom_ok.iloc[i]):
                p = float(rs_sym.iloc[i]) if rs_sym is not None and not math.isnan(rs_sym.iloc[i]) else None
                sub = d.iloc[:i + 1]
                hit = setups.scan_momentum(sym, sub, p)
                if hit:
                    found[day].append(dict(hit, i=i))
            if bool(bnc_ok.iloc[i]):
                sub = d.iloc[:i + 1] if sub is None else sub
                hit = setups.scan_bounce(sym, sub)
                if hit:
                    found[day].append(dict(hit, i=i))
    return found


# ── one trade ────────────────────────────────────────────────────────────
def simulate(d, cand, variant, regime=None):
    """Take one candidate and run it to its exit. Returns a dict or None (never filled)."""
    i = cand["i"]
    n = len(d)
    if i + 1 >= n:
        return None
    setup = cand["setup"]

    if regime is not None:                                # live skips setups the regime blocks
        reg = regime.get(d.index[i + 1].date(), "full")
        if risk.regime_mult(reg, setup) <= 0:
            return None
    if cand["entry"] == "breakout":
        nxt = d.iloc[i + 1]
        if nxt.high < cand["trigger"]:
            return None                                   # never triggered: no trade
        if VOL_CONFIRM:                                   # live only buys a breakout on volume
            need = {"momentum": R.MOMENTUM, "catalyst": R.CATALYST}.get(setup, {}).get("vol_mult")
            v50 = _f(d.iloc[i].vol50)
            if need and v50 and float(nxt.volume) < need * v50:
                return None
        fill = max(float(nxt.open), cand["trigger"])
        if fill > cand["trigger"] * (1 + R.MOMENTUM["max_extension"]):
            return None                                   # gapped away, live would not chase
        entry_i = i + 1
        stop = cand["stop"]
    else:                                                 # bounce: confirmed near the next close
        entry_i = i + 1
        price = float(d["close"].iloc[entry_i])
        ok, stop, _ = setups.bounce_confirm(d.iloc[:i + 1], price)
        if not ok:
            return None
        fill = price
    fill *= 1 + SLIPPAGE
    rps = fill - stop
    if rps <= 0:
        return None

    hold = R.BOUNCE["max_hold_days"] if setup == "bounce" else variant["max_hold"]
    entry_day = d.index[entry_i].date()
    pos = dict(symbol=cand["symbol"], setup=setup, entry_price=fill, risk_per_share=rps,
               stop=stop, qty=UNITS, partial_done=0, high_water=fill, entry_date=str(entry_day),
               max_exit_date=str(risk.add_trading_days(entry_day, hold)) if hold else None)
    realized, qty = 0.0, UNITS
    for j in range(entry_i + 1, min(n, entry_i + 1 + MAX_TRADE_DAYS)):
        bar, prev = d.iloc[j], d.iloc[j - 1]
        if bar.low <= pos["stop"]:                        # stop first: the pessimistic assumption
            px = min(float(bar.open), pos["stop"]) * (1 - SLIPPAGE)
            realized += qty * (px - fill)
            return _result(cand, pos, realized, rps, j - entry_i, "stop", j, d.index[j])
        ctx = dict(close=float(prev.close), low10=_f(prev.low10), low20=_f(prev.low20),
                   ma5=_f(prev.ma5), ma50=_f(prev.ma50))
        pos["high_water"] = max(pos["high_water"], float(prev.close))
        px = float(bar.close)
        for kind, arg in risk.exit_signals(pos, px, ctx, d.index[j].date(), 100_000.0, None):
            if kind == "exit":
                realized += qty * (px * (1 - SLIPPAGE) - fill)
                return _result(cand, pos, realized, rps, j - entry_i, str(arg), j, d.index[j])
            if kind == "partial":
                part = min(float(arg), qty)
                realized += part * (px * (1 - SLIPPAGE) - fill)
                qty -= part
                pos["qty"], pos["partial_done"] = qty, 1
            if kind == "raise_stop":
                pos["stop"] = arg
    j = min(n, entry_i + 1 + MAX_TRADE_DAYS) - 1
    realized += qty * (float(d["close"].iloc[j]) * (1 - SLIPPAGE) - fill)
    return _result(cand, pos, realized, rps, j - entry_i, "still open at the end of the data",
                   j, d.index[j])


def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _result(cand, pos, realized, rps, days, reason, exit_i, exit_date):
    return dict(symbol=cand["symbol"], setup=cand["setup"], date=pos["entry_date"],
                r=realized / (rps * UNITS), days=days, reason=reason, exit_i=exit_i,
                exit_date=str(exit_date.date()))


# ── variants ─────────────────────────────────────────────────────────────
BASE = dict(name="live rules (runner mode)", breakeven=R.BREAKEVEN_AT_R, partial=3.0,
            runner=3.0, max_hold=15, time_stop=7, trail=10)
VARIANTS = [
    BASE,
    dict(BASE, name="no runner mode", runner=9e9),
    dict(BASE, name="no breakeven stop", breakeven=9e9),
    dict(BASE, name="no partial sale", partial=9e9),
    dict(BASE, name="partial at +2R (old)", partial=2.0),
    dict(BASE, name="old rules (2R partial, no runner)", partial=2.0, runner=9e9),
    dict(BASE, name="runner at +2R", runner=2.0),
    dict(BASE, name="runner at +4R", runner=4.0),
    dict(BASE, name="breakeven at +1.5R", breakeven=1.5),
    dict(BASE, name="no time stop, no max hold", max_hold=None, time_stop=9e9),
    dict(BASE, name="longer leash (hold 25, time stop 10)", max_hold=25, time_stop=10),
]


def _apply(v):
    R.BREAKEVEN_AT_R = v["breakeven"]
    R.PARTIAL_AT_R = v["partial"]
    R.RUNNER_AT_R = v["runner"]
    R.MOMENTUM = dict(R.MOMENTUM, time_stop_days=v["time_stop"], trail_low_days=v["trail"])
    R.CATALYST = dict(R.CATALYST, time_stop_days=v["time_stop"])


def stats(trades):
    if not trades:
        return dict(n=0)
    rs = np.array([t["r"] for t in trades])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    return dict(n=len(rs), win_pct=100 * len(wins) / len(rs), expectancy=float(rs.mean()),
                total_r=float(rs.sum()), avg_win=float(wins.mean()) if len(wins) else 0.0,
                avg_loss=float(losses.mean()) if len(losses) else 0.0,
                best=float(rs.max()), worst=float(rs.min()),
                pf=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() else float("inf"),
                over_3r=int((rs >= 3).sum()), days=float(np.mean([t["days"] for t in trades])))


def _line(label, s):
    if not s.get("n"):
        return f"{label:38} no trades"
    return (f"{label:38} {s['n']:5d} trades  win {s['win_pct']:4.1f}%  "
            f"exp {s['expectancy']:+.3f}R  total {s['total_r']:+8.1f}R  "
            f"avg win {s['avg_win']:+.2f} / loss {s['avg_loss']:+.2f}  PF {s['pf']:.2f}  "
            f">=3R {s['over_3r']:3d}  {s['days']:.0f}d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=0, help="limit universe (for a quick run)")
    ap.add_argument("--top", type=int, default=3, help="top N signals per day = the live-like subset")
    ap.add_argument("--out", default="data/backtest.json")
    ap.add_argument("--no-vol", action="store_true", help="ignore the volume confirmation")
    ap.add_argument("--only", default="", help="run one variant by name")
    ap.add_argument("--no-regime", action="store_true", help="ignore the SPY regime filter")
    a = ap.parse_args()
    global VOL_CONFIRM
    VOL_CONFIRM = not a.no_vol

    global REGIME
    print("loading bars...", flush=True)
    frames = _frames(a.symbols or None)
    print(f"{len(frames)} symbols with usable history", flush=True)
    REGIME = None if a.no_regime else spy_regime(frames)
    if REGIME:
        n_full = sum(1 for v in REGIME.values() if v == "full")
        print(f"regime: {n_full}/{len(REGIME)} days 'full' (momentum allowed)", flush=True)
    rs = _rs_percentiles(frames)
    print("scanning history...", flush=True)
    cands = candidates(frames, rs, R.MIN_HISTORY_BARS)
    days = sorted(cands)
    total = sum(len(v) for v in cands.values())
    print(f"{total} signals over {len(days)} days ({days[0].date()} to {days[-1].date()})", flush=True)

    half = days[len(days) // 2]
    ranked = {d: sorted(v, key=lambda c: -c["score"])[:a.top] for d, v in cands.items()}
    out = {}
    for v in ([x for x in VARIANTS if x["name"] == a.only] or VARIANTS) if a.only else VARIANTS:
        _apply(v)
        all_t, top_t = [], []
        # One position per symbol at a time, and the 31-day wash-sale block after a loss,
        # exactly as the live engine does it. Without this the same coiling stock produces
        # a fresh "trade" every day and one failed breakout gets counted five times.
        busy, blocked = {}, {}
        busy_top, blocked_top = {}, {}
        for day in days:
            keep = {id(c) for c in ranked[day]}
            for c in cands[day]:
                sym, di = c["symbol"], c["i"]
                in_all = busy.get(sym, -1) < di and blocked.get(sym, "") <= str(day.date())
                in_top = (id(c) in keep and busy_top.get(sym, -1) < di
                          and blocked_top.get(sym, "") <= str(day.date()))
                if not (in_all or in_top):
                    continue
                t = simulate(frames[sym], c, v, REGIME)
                if not t:
                    continue
                block_to = str(pd.Timestamp(t["exit_date"]).date() +
                               pd.Timedelta(days=R.NO_REBUY_AFTER_LOSS_DAYS)) if t["r"] < 0 else ""
                if in_all:
                    all_t.append(t)
                    busy[sym] = t["exit_i"]
                    if block_to:
                        blocked[sym] = block_to
                if in_top:
                    top_t.append(t)
                    busy_top[sym] = t["exit_i"]
                    if block_to:
                        blocked_top[sym] = block_to
        by_setup = defaultdict(list)
        for t in all_t:
            by_setup[t["setup"]].append(t)
        first = [t for t in all_t if t["date"] < str(half.date())]
        second = [t for t in all_t if t["date"] >= str(half.date())]
        out[v["name"]] = dict(all=stats(all_t), top=stats(top_t), first_half=stats(first),
                              second_half=stats(second),
                              by_setup={k: stats(t) for k, t in by_setup.items()},
                              by_setup_half={k: dict(
                                  first=stats([t for t in ts if t["date"] < str(half.date())]),
                                  second=stats([t for t in ts if t["date"] >= str(half.date())]))
                                  for k, ts in by_setup.items()})
        print(f"\n=== {v['name']}")
        print(_line("  every signal", out[v["name"]]["all"]))
        print(_line(f"  top {a.top}/day (live-like)", out[v["name"]]["top"]))
        print(_line("    first half", out[v["name"]]["first_half"]))
        print(_line("    second half", out[v["name"]]["second_half"]))
        for k, st in sorted(out[v["name"]]["by_setup"].items()):
            print(_line(f"    {k}", st))
            h = out[v["name"]]["by_setup_half"][k]
            print(_line(f"      {k} 1st half", h["first"]))
            print(_line(f"      {k} 2nd half", h["second"]))
        sys.stdout.flush()
    _apply(BASE)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"\nwritten to {a.out}")


if __name__ == "__main__":
    main()
