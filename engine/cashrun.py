"""CashMoney, run once and reported. Never places an order.

    CLAUDIO_PROFILE=cash CLAUDIO_DB=data/cash.db venv312/bin/python -m engine.cashrun

Until the owner grants this engine access to the account, there is nothing to place
orders against and nothing to report a balance from, so it runs on a stated notional
and produces the memo it would produce live: the monthly core decision, what the value
screen found, what each position would be, and what the idle cash is costing. The
moment the account is granted the same code reads the real balance instead.

  --notional 25000   account size to plan against when the account isn't granted yet
  --email            send the memo instead of only printing it
  --no-refresh       skip the data refresh (fast, uses whatever is cached)
"""
import argparse
import math
import sys
from datetime import date

from . import analyst, cashcore, data, db, indicators, risk, setups
from . import rules as R
from schwab_api import config as scfg

DIVIDER = "-" * 72


def _equity(notional):
    """Real balance if the account has been granted, otherwise the stated notional."""
    try:
        a = data.account()
        return dict(equity=a["equity"], cash=a["settled_cash"], positions=a["positions"],
                    live=True, last4=a["last4"], excluded=a["excluded"])
    except Exception as e:
        return dict(equity=float(notional), cash=float(notional), positions={},
                    live=False, last4=None, excluded=sorted(scfg.EXCLUDED_SYMBOLS),
                    why=f"{type(e).__name__}: {e}")


def _core_syms():
    return {R.CORE["symbol"]} if R.CORE else set()


def _frames(symbols, progress):
    out = {}
    for sym in symbols:
        d = db.load_bars(sym, 420)
        if len(d) >= R.MIN_HISTORY_BARS:
            out[sym] = indicators.enrich(d)
    progress(f"enriched {len(out)} symbols")
    return out


def _sector_medians(funds, uni):
    by = {}
    for sym, f in funds.items():
        sec = uni.get(sym, {}).get("sector")
        if sec and f.get("peRatio") and 0 < f["peRatio"] < 200:
            by.setdefault(sec, []).append(f["peRatio"])
    return {k: sorted(v)[len(v) // 2] for k, v in by.items() if len(v) >= 10}


def _spy_gap(spy):
    if spy is None or spy.empty:
        return None
    t = spy.iloc[-1]
    return round((t.close / t.ma200 - 1) * 100, 1)


def _book(acct, dbpos, core_sym):
    """The engine's own holdings.

    Fenced sleeves -- the owner's index core, the Treasury sleeve -- are not the engine's
    book. They carry no stop by design, and a holding with no stop counts its whole value
    as open risk, so leaving them in here charges ~18% of equity against an 8% budget and
    blocks every entry the screen will ever find.
    """
    out = []
    for s, h in acct["positions"].items():
        if s in acct["excluded"]:
            continue
        mp = dbpos.get(s, {})
        out.append(risk.Holding(
            symbol=s, qty=h["qty"], price=h["price"], stop=mp.get("stop"),
            sector=mp.get("sector") or "", market_cap=mp.get("market_cap"),
            # a holding the engine has not reviewed yet is "legacy", not "value": it was
            # not bought under the value rules and must not consume the value budget.
            setup=mp.get("setup") or ("core" if s == core_sym else "legacy")))
    return out


def plan(notional=25_000, refresh=True, progress=print, ai=True):
    """Everything CashMoney would do today, as data. Pure reporting."""
    db.init()                       # this profile has its own database; make sure it exists
    if db.MARKET_READONLY:
        # The market data belongs to the other engine, which refreshes it nightly.
        # Borrowing it is the whole point; writing to it is not ours to do.
        if refresh:
            progress(f"market data is read-only ({db.MARKET_DB_PATH}); skipping refresh")
        refresh = False
    today = data.now_et().date()
    acct = _equity(notional)
    uni = db.get_universe()
    if refresh:
        if not uni or not db.get_state("universe_refreshed"):
            data.refresh_universe()
            db.set_state("universe_refreshed", str(today))
            uni = db.get_universe()
        syms = sorted(set(uni) | {"SPY"} | _core_syms() | set(acct["positions"]))
        data.update_bars(syms, progress=lambda n: progress(f"bars {n}/{len(syms)}"))
        last_f = db.get_state("fundamentals_refreshed")
        if not last_f or (today - date.fromisoformat(last_f)).days >= 6:
            data.refresh_fundamentals([s for s in uni if uni[s].get("sector") != "ETF"])
            db.set_state("fundamentals_refreshed", str(today))
    frames = _frames(sorted(set(uni) | {"SPY"} | _core_syms() | set(acct["positions"])), progress)

    # market regime: this account is allowed to be mostly cash for months
    spy = frames.get("SPY")
    reg = "unknown"
    if spy is not None:
        s = spy.iloc[-1]
        vix = (data.quotes(["$VIX"]).get("$VIX") or {}).get("last") if refresh else None
        reg = risk.regime(s.close, s.ma50, s.ma200, vix)

    # the core: only when this profile has one. The owner holds his own index core, so
    # normally R.CORE is None and every branch below is skipped.
    core_sym = R.CORE["symbol"] if R.CORE else None
    core_d = frames.get(core_sym) if core_sym else None
    core_px = None
    if core_d is not None:
        core_px = float(core_d.iloc[-1].close)
        if refresh:
            q = data.quotes([core_sym]).get(core_sym) or {}
            core_px = q.get("last") or core_px
    held_core = acct["positions"].get(core_sym, {}).get("qty", 0) if core_sym else 0
    core = cashcore.core_plan(core_d, today, held_core, core_px or 0.0, acct["equity"],
                              last_month_done=db.get_state("core_month_done"))

    # satellites already owned: does the trend still hold?
    breaks = []
    for sym in acct["positions"]:
        if sym == core_sym or sym in acct["excluded"]:
            continue
        b = cashcore.trend_break(frames.get(sym), today)
        if b:
            breaks.append(sym)

    # the screen
    funds = db.get_fundamentals()
    sector_pe = _sector_medians(funds, uni)
    held = set(acct["positions"])
    cands = []
    for sym, d in frames.items():
        u = uni.get(sym, {})
        if sym in held or u.get("sector") == "ETF" or sym in acct["excluded"]:
            continue
        t = d.iloc[-1]
        if not (t.close >= R.MIN_PRICE and t.dollar_vol50 >= R.MIN_DOLLAR_VOLUME):
            continue
        if (u.get("market_cap") or 0) < R.MIN_MCAP:
            continue
        f = funds.get(sym)
        if not f:
            continue
        c = setups.scan_value(sym, d, f, sector_pe.get(u.get("sector")))
        if c:
            c.update(sector=u.get("sector"), industry=u.get("industry"),
                     market_cap=u.get("market_cap"))
            cands.append(c)
    cands.sort(key=lambda c: -c["score"])

    # what each one would actually be, at this account size
    # a holding with no stop counts its whole value as risk, so the real stops matter
    dbpos = db.get_positions()
    holdings = _book(acct, dbpos, core_sym)

    core_usd = cashcore.core_cash_reserve(core["action"], core["qty"], core_px,
                                          held_core, acct["equity"])
    snap = risk.Snapshot(equity=acct["equity"],
                         settled_cash=max(0.0, acct["cash"] - core_usd),
                         holdings=holdings, regime=reg if reg != "unknown" else "full")
    sized = []
    for c in cands[:R.PM_MAX_CANDIDATES]:
        qty, why = risk.size_trade(snap, c["symbol"], "value", c["trigger"], c["stop"],
                                   c.get("sector"), c.get("market_cap"))
        sized.append(dict(c, qty=qty, why=why,
                          cost=round(qty * c["trigger"], 2),
                          risk=round(qty * (c["trigger"] - c["stop"]), 2)))

    idle = max(0.0, acct["cash"] - core_usd - sum(s["cost"] for s in sized if s["qty"]))

    # ── the last step: nothing is bought that the portfolio manager has not approved ──
    affordable = [s for s in sized if s["qty"]]
    capacity = min(R.MAX_NEW_ENTRIES_PER_DAY, len(affordable))
    pm, pm_error = None, None
    if ai and affordable:
        book = [dict(symbol=h.symbol, setup=h.setup or "unreviewed", qty=h.qty,
                     price=round(h.price, 2), stop=h.stop,
                     pct_of_sleeve=round(h.value / snap.equity * 100, 1) if snap.equity else None)
                for h in holdings]
        payload = dict(
            date=str(today), account=dict(
                equity=round(acct["equity"], 2), settled_cash=round(acct["cash"], 2),
                open_risk_pct=round(snap.open_risk() / snap.equity * 100, 2) if snap.equity else 0,
                open_risk_cap_pct=R.MAX_OPEN_RISK * 100, positions=snap.count(),
                max_positions=R.MAX_POSITIONS, capacity=capacity),
            market=dict(regime=reg, spy_vs_ma200_pct=_spy_gap(spy)),
            holdings=book,
            candidates=[{k: v for k, v in c.items() if k not in ("_c",)} for c in affordable],
            trend_broken=breaks,
            previous_memos=(db.get_state("cash_memos", []) or [])[-R.PM_MEMO_HISTORY:])
        progress(f"portfolio manager reviewing {len(affordable)} candidates")
        pm = analyst.cash_review(payload)
        if pm is None:
            pm_error = ("the portfolio manager call failed, so nothing is approved tonight. "
                        "The screen's output below is raw and has not been judged.")
        else:
            memos = (db.get_state("cash_memos", []) or []) + [dict(date=str(today), memo=pm["memo"])]
            db.set_state("cash_memos", memos[-30:])
            db.log("cash pm", None, memo=pm["memo"],
                   entries=[e["symbol"] for e in pm["entries"]], vetoes=pm["vetoes"])

    return dict(today=str(today), acct=acct, regime=reg, core=core, core_price=core_px,
                breaks=breaks, candidates=sized, screened=len(cands), capacity=capacity,
                pm=pm, pm_error=pm_error, reviewed=bool(ai),
                idle_note=cashcore.idle_cash_note(idle), idle=round(idle, 2))


def _wrap(text, width):
    out, line = [], ""
    for word in str(text).split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]


def render(p):
    a = p["acct"]
    L = []
    L.append(f"CashMoney plan - {p['today']}")
    L.append(DIVIDER)
    if a["live"]:
        L.append(f"Account ...{a['last4']}: managed equity ${a['equity']:,.0f}, "
                 f"settled cash ${a['cash']:,.0f}")
    else:
        L.append(f"ADVISORY ONLY - the account has not been granted to this engine yet.")
        L.append(f"Planning against a stated ${a['equity']:,.0f}. No order can be placed.")
    if a["excluded"]:
        L.append(f"Your own holdings ({', '.join(a['excluded'])}) are excluded from equity "
                 f"and are never traded here.")
    L.append(f"Market regime: {p['regime']}"
             + ("  (below the 200-day: no new satellite buying)" if p["regime"] == "risk_off" else ""))
    L.append("")
    if R.CORE:
        L.append(f"CORE - {R.CORE['symbol']}, target {R.CORE['target_pct']:.0%} of equity")
        c = p["core"]
        L.append(f"  {c['action'].upper()}"
                 + (f" {c['qty']} sh" if c["qty"] else "")
                 + f"  |  {c['reason']}")
        L.append("")
    if p["breaks"]:
        L.append(f"TREND BROKEN (sell at the next open): {', '.join(p['breaks'])}")
        L.append("")
    pm = p.get("pm")
    if p.get("pm_error"):
        L.append("!! " + p["pm_error"])
        L.append("")
    elif pm:
        L.append("PORTFOLIO MANAGER")
        for line in _wrap(pm["memo"], 70):
            L.append("  " + line)
        L.append("")
        if pm["entries"]:
            L.append("  APPROVED:")
            for e in pm["entries"]:
                half = " (half size)" if e["size_mult"] < 1 else ""
                L.append(f"    {e['symbol']}{half} - {e['thesis']}")
                L.append(f"      invalidated if: {e['invalidation']}")
        else:
            L.append("  APPROVED: nothing. No new position tomorrow.")
        for v in pm["vetoes"]:
            L.append(f"  VETOED {v['symbol']}: {v['reason']}")
        L.append("")
    elif not p.get("reviewed"):
        L.append("(portfolio manager review skipped: --no-ai)")
        L.append("")

    L.append(f"WHAT THE SCREEN FOUND - {p['screened']} names passed "
             f"(cheap vs sector, growing, not a cycle peaking, above a flat-or-rising 200-day)")
    if not p["candidates"]:
        L.append("  Nothing qualifies today. That is a normal week in this account.")
    for s in p["candidates"]:
        head = f"  {s['symbol']:<6} buy above ${s['trigger']:.2f}, stop ${s['stop']:.2f} " \
               f"({(s['trigger'] - s['stop']) / s['trigger']:.1%} away)"
        if s["qty"]:
            head += f"  ->  {s['qty']} sh, ${s['cost']:,.0f}, risking ${s['risk']:,.0f}"
        else:
            head += f"  ->  no trade: {s['why']}"
        L.append(head)
        L.append(f"         {s.get('note', '')}")
    L.append("")
    if p["idle_note"]:
        L.append(p["idle_note"])
    L.append(DIVIDER)
    L.append(f"Limits (of the managed sleeve, not the account): {R.RISK_PER_TRADE:.0%} risk "
             f"per position, {R.MAX_OPEN_RISK:.0%} total for new trades, "
             f"max {R.MAX_POSITIONS} positions at {R.MAX_POSITION_PCT:.0%}, "
             f"{R.MAX_NEW_ENTRIES_PER_DAY}/day and {R.MAX_NEW_ENTRIES_PER_MONTH}/month, "
             f"halt at -{R.TOTAL_DRAWDOWN_HALT:.0%} from peak.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="CashMoney plan (advisory, never trades)")
    ap.add_argument("--notional", type=float, default=25_000)
    ap.add_argument("--email", action="store_true")
    ap.add_argument("--no-refresh", dest="refresh", action="store_false")
    ap.add_argument("--no-ai", dest="ai", action="store_false",
                    help="skip the portfolio manager review (screen output only, spends no credits)")
    args = ap.parse_args(argv)
    if R.PROFILE != "cash":
        sys.exit("Run this with CLAUDIO_PROFILE=cash (and its own CLAUDIO_DB).")
    p = plan(args.notional, refresh=args.refresh, ai=args.ai,
             progress=lambda m: print(m, file=sys.stderr))
    text = render(p)
    print(text)
    if args.email:
        from . import mailer
        mailer.send(f"CashMoney plan {p['today']}",
                    mailer.page("CashMoney plan", f"<pre>{mailer.esc(text)}</pre>"), text)
    return p


if __name__ == "__main__":
    main()
