"""Scorekeeping: is this working, and is the AI layer earning its place?

Three questions, three functions:

  attribution()  Did the AI's picking beat the scanner's ranking? Every candidate the
                 portfolio manager saw is written to the shadow book with what the AI
                 decided (taken / vetoed / passed over). Once enough days have passed,
                 each one is replayed against the bars under the live exit rules, so the
                 trades the AI rejected get a score too. Comparing the three groups is
                 the only way to tell whether the AI adds anything, and nothing here can
                 flatter it: the replay uses the same code for all three.

  benchmark()    Account against SPY buy-and-hold since the engine went live. "Up 4%"
                 means nothing if the index did 6%.

  execution()    What fills actually cost. Every backtest number rests on an assumed
                 0.15% slippage; this measures the real thing, so the assumption can be
                 checked instead of believed.

  scorecard()    Expectancy in R, win rate, average win and loss, profit factor. Overall,
                 by setup, and for the most recent trades. A strategy is working when the
                 average win times the win rate beats the average loss times the loss
                 rate - not when the last trade was green.

Nothing here places orders or calls the AI. Safe to run any time.
"""
import math
from datetime import date, timedelta

from . import db, indicators
from . import rules as R

SHADOW_MIN_AGE_DAYS = 21      # let a candidate play out before scoring it
MIN_FOR_VERDICT = 30          # below this, report the numbers but call them undecided


# ── AI attribution ───────────────────────────────────────────────────────
def score_pending(today=None):
    """Replay every unscored shadow candidate old enough to have finished. Returns how many."""
    from . import backtest
    today = today or date.today()
    cutoff = str(today - timedelta(days=SHADOW_MIN_AGE_DAYS))
    rows = db.shadow(unscored_before=cutoff)
    if not rows:
        return 0
    variant = dict(backtest.BASE)
    frames, n = {}, 0
    for row in rows:
        sym = row["symbol"]
        if sym not in frames:
            d = db.load_bars(sym, 400)
            frames[sym] = indicators.enrich(d) if len(d) > R.MIN_HISTORY_BARS else None
        d = frames[sym]
        if d is None:
            db.score_shadow(row["date"], sym, None, None, "no bars")
            continue
        idx = [i for i, ts in enumerate(d.index) if str(ts.date()) == row["date"]]
        if not idx:
            db.score_shadow(row["date"], sym, None, None, "no bar for that day")
            continue
        cand = dict(symbol=sym, setup=row["setup"], entry="near_close" if row["setup"] == "bounce"
                    else "breakout", trigger=row["trigger"], stop=row["stop"],
                    score=row["score"], i=idx[0])
        try:
            t = backtest.simulate(d, cand, variant)
        except Exception as e:                       # never let scorekeeping break a report
            db.score_shadow(row["date"], sym, None, None, f"error: {str(e)[:40]}")
            continue
        if t is None:
            db.score_shadow(row["date"], sym, None, None, "never triggered")
        else:
            db.score_shadow(row["date"], sym, round(t["r"], 3), t["days"], t["reason"])
        n += 1
    return n


def _agg(rows):
    rs = [r["r"] for r in rows if r.get("r") is not None]
    if not rs:
        return dict(n=len(rows), traded=0)
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    return dict(n=len(rows), traded=len(rs), win_pct=round(100 * len(wins) / len(rs), 1),
                expectancy=round(sum(rs) / len(rs), 3), total_r=round(sum(rs), 1),
                avg_win=round(sum(wins) / len(wins), 2) if wins else 0.0,
                avg_loss=round(sum(losses) / len(losses), 2) if losses else 0.0,
                best=round(max(rs), 2), worst=round(min(rs), 2))


def attribution(since=None):
    """{taken, vetoed, passed} expectancy, plus the verdict on the AI layer."""
    rows = [r for r in db.shadow(since=since) if r.get("scored")]
    groups = {k: _agg([r for r in rows if r["decision"] == k])
              for k in ("taken", "vetoed", "passed")}
    took, rest = groups["taken"], _agg([r for r in rows if r["decision"] != "taken"])
    verdict, edge = "not enough data yet", None
    if took.get("traded", 0) >= MIN_FOR_VERDICT and rest.get("traded", 0) >= MIN_FOR_VERDICT:
        edge = round(took["expectancy"] - rest["expectancy"], 3)
        verdict = ("the AI's picks beat the ones it passed on" if edge > 0.05 else
                   "the AI's picks are worse than the ones it passed on" if edge < -0.05 else
                   "no measurable difference between the AI's picks and the rest")
    return dict(groups=groups, rest=rest, edge_r=edge, verdict=verdict,
                scored=len(rows), min_for_verdict=MIN_FOR_VERDICT)


# ── benchmark ────────────────────────────────────────────────────────────
def benchmark(curve=None):
    """Account vs SPY buy-and-hold over the same days. None if there isn't a curve yet."""
    curve = curve if curve is not None else (db.get_state("equity_curve", []) or [])
    curve = [c for c in curve if c and c[1]]
    if len(curve) < 2:
        return None
    spy = db.load_bars("SPY", 400)
    if spy.empty:
        return None
    px = {str(ts.date()): float(v) for ts, v in spy["close"].items()}
    start, end = curve[0], curve[-1]
    s0 = next((px[d] for d, _ in curve if d in px), None)
    s1 = next((px[d] for d, _ in reversed(curve) if d in px), None)
    if not (s0 and s1):
        return None
    me = end[1] / start[1] - 1
    bench = s1 / s0 - 1
    return dict(since=start[0], days=len(curve), equity=round(end[1], 2),
                me_pct=round(me * 100, 2), spy_pct=round(bench * 100, 2),
                diff_pct=round((me - bench) * 100, 2))


# ── scorecard ────────────────────────────────────────────────────────────
def _stats(ts):
    rs = [t["r_multiple"] for t in ts if t.get("r_multiple") is not None]
    if not rs:
        return dict(n=len(ts))
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return dict(n=len(rs), win_pct=round(100 * len(wins) / len(rs), 1),
                expectancy=round(sum(rs) / len(rs), 3), total_r=round(sum(rs), 1),
                avg_win=round(gross_win / len(wins), 2) if wins else 0.0,
                avg_loss=round(-gross_loss / len(losses), 2) if losses else 0.0,
                pf=round(gross_win / gross_loss, 2) if gross_loss else None,
                pnl=round(sum(t["pnl"] or 0 for t in ts), 2))


def scorecard(last=20):
    all_t = db.trades()
    by_setup = {}
    for t in all_t:
        by_setup.setdefault(t["setup"], []).append(t)
    need = R.SETUP_REVIEW_MIN_TRADES
    return dict(all=_stats(all_t), recent=_stats(all_t[-last:]),
                by_setup={k: _stats(v) for k, v in sorted(by_setup.items())},
                enough_data=len(all_t) >= 100,
                flagged=[k for k, v in by_setup.items()
                         if len(v) >= need and (_stats(v).get("expectancy") or 0) < 0])


# ── execution quality ────────────────────────────────────────────────────
def execution():
    """Entry and stop slippage in basis points. Negative = worse than intended."""
    log = db.get_state("slippage", {}) or {}
    out = {}
    for kind in ("entry", "stop"):
        rows = log.get(kind) or []
        bps = [r["bps"] for r in rows if r.get("bps") is not None]
        if not bps:
            continue
        worst = min(rows, key=lambda r: r["bps"])
        out[kind] = dict(n=len(bps), avg_bps=round(sum(bps) / len(bps), 1),
                         median_bps=round(sorted(bps)[len(bps) // 2], 1),
                         worst_bps=round(worst["bps"], 1), worst_symbol=worst["symbol"],
                         assumed_bps=-15.0)
    return out


def execution_note():
    """One sentence for the weekly email, in plain words."""
    e = execution()
    if not e:
        return ""
    bits = []
    if "entry" in e:
        x = e["entry"]
        bits.append(f"entries filled {abs(x['avg_bps']):.0f} bps "
                    f"{'worse' if x['avg_bps'] < 0 else 'better'} than the trigger "
                    f"over {x['n']} fills (the backtest assumed 15)")
    if "stop" in e:
        x = e["stop"]
        bits.append(f"stops filled {abs(x['avg_bps']):.0f} bps "
                    f"{'below' if x['avg_bps'] < 0 else 'above'} the stop price over "
                    f"{x['n']} (worst: {x['worst_symbol']} at {x['worst_bps']:.0f})")
    return "Execution: " + "; ".join(bits) + "."


def summary_line():
    """One line for the daily email: where we stand against the index."""
    b = benchmark()
    if not b:
        return ""
    sign = "ahead of" if b["diff_pct"] >= 0 else "behind"
    return (f"Since {b['since']}: account {b['me_pct']:+.2f}%, SPY {b['spy_pct']:+.2f}% "
            f"({abs(b['diff_pct']):.2f} points {sign} the index).")
