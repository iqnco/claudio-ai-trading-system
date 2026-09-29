"""The trading day: nightly scan, intraday management, near-close checks."""
import json
import math
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta

from . import analyst, broker, data, db, indicators, metrics, notify, risk, setups
from . import rules as R

SETUP_PARAMS = {"momentum": R.MOMENTUM, "bounce": R.BOUNCE, "catalyst": R.CATALYST,
                "value": R.VALUE, "legacy": {}}


def today_et():
    return data.now_et().date()


def _held_bars(symbol):
    d = db.load_bars(symbol, 300)
    return indicators.enrich(d) if len(d) >= 30 else None


def _bar_ctx(d):
    t = d.iloc[-1]
    f = lambda x: None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    return dict(low10=f(t.low10), low20=f(t.low20), ma5=f(t.ma5), ma50=f(t.ma50),
                ma200=f(t.ma200), atr=f(t.atr), vol50=f(t.vol50), close=f(t.close))


# ── snapshot ─────────────────────────────────────────────────────────────
def snapshot(acct, prices=None):
    prices = prices or {}
    dbpos = db.get_positions()
    uni = db.get_universe()
    holdings = []
    for sym, p in acct["positions"].items():
        mp = dbpos.get(sym, {})
        u = uni.get(sym, {})
        holdings.append(risk.Holding(
            symbol=sym, qty=p["qty"], price=prices.get(sym) or p["price"],
            stop=mp.get("stop"), sector=mp.get("sector") or u.get("sector", ""),
            market_cap=mp.get("market_cap") or u.get("market_cap"), setup=mp.get("setup", "")))
    working = [o for o in db.open_orders("entry")]
    today = str(today_et())
    entries_today = sum(1 for o in db.open_orders("entry") if o["created"][:10] == today) + \
        db.get_state(f"filled_entries_{today}", 0)
    # CashMoney limits new positions per month as well as per day; Luck leaves this at None.
    month = today[:7]
    entries_this_month = sum(1 for o in db.open_orders("entry") if o["created"][:7] == month) + \
        sum(v for k, v in (db.all_state_prefix("filled_entries_") or {}).items()
            if k[len("filled_entries_"):][:7] == month and isinstance(v, int))
    halt = "ok"
    if db.get_state("halt_manual"):
        halt = "manual"
    elif db.get_state("halt_full"):
        halt = "full_halt"
    else:
        halt = risk.kill_switch(acct["equity"], db.get_state("day_start_equity"),
                                db.get_state("peak_equity"))
    return risk.Snapshot(
        equity=acct["equity"], settled_cash=acct["settled_cash"], holdings=holdings,
        reserved_cash=sum((o["price"] or 0) * o["qty"] for o in working),
        pending_entries=len(working), entries_today=entries_today,
        entries_this_month=entries_this_month,
        regime=db.get_state("regime", "full"), halt=halt)


def publish_status(acct, snap):
    """What the Telegram bot shows. The bot reads this; it never trades."""
    dbpos = db.get_positions()
    rows = []
    for h in snap.holdings:
        p = dbpos.get(h.symbol, {})
        rps = p.get("risk_per_share")
        r_now = (h.price - p["entry_price"]) / rps if rps and p.get("entry_price") else None
        rows.append(dict(symbol=h.symbol, qty=h.qty, price=round(h.price, 2),
                         value=round(h.value, 2), stop=h.stop, setup=h.setup or "unreviewed",
                         r=round(r_now, 2) if r_now is not None else None,
                         pct=round(h.value / snap.equity * 100, 1)))
    db.set_state("status", dict(
        ts=data.now_et().isoformat(timespec="minutes"), equity=round(acct["equity"], 2),
        settled_cash=round(acct["settled_cash"], 2), heat=round(snap.open_risk() / snap.equity * 100, 2),
        regime=snap.regime, halt=snap.halt, positions=sorted(rows, key=lambda r: -r["value"]),
        day_start=db.get_state("day_start_equity"), peak=db.get_state("peak_equity"),
        live=broker.live_enabled()))


# ── reconciliation ───────────────────────────────────────────────────────
def reconcile(acct):
    """Make local state agree with Schwab. Schwab wins."""
    live = broker.live_enabled()
    dbpos = db.get_positions()
    opens = db.open_orders()
    omap = {}
    if live:
        omap = broker.order_map(acct["hash"], [p.get("stop_order_id") for p in dbpos.values()] +
                                [o["order_id"] for o in opens])
        for o in opens:
            if o["order_id"].startswith("DRY"):
                continue
            s = (omap.get(o["order_id"]) or {}).get("status")
            if s and s != o["status"]:
                db.set_order_status(o["order_id"], s)
    held = acct["positions"]
    today = str(today_et())

    # 1. filled entries -> new managed positions
    if live:
        with db.conn() as c:
            filled = [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE kind='entry' AND status='FILLED'")]
        for o in filled:
            sym = o["symbol"]
            if sym in dbpos or sym not in held:
                continue
            meta = json.loads(o["meta"] or "{}")
            om = omap.get(o["order_id"], {})
            child = next((k for k, v in omap.items() if v.get("_parent") == o["order_id"]), None)
            px = broker.fill_price(om) or held[sym]["avg"]
            stop = o["stop"]
            setup = meta.get("setup", "momentum")
            hold = SETUP_PARAMS.get(setup, {}).get("max_hold_days")
            db.upsert_position(dict(
                symbol=sym, setup=setup, entry_date=today, entry_price=px, qty=held[sym]["qty"],
                initial_stop=stop, stop=stop, risk_per_share=max(0.01, px - stop),
                high_water=px, stop_order_id=child, sector=meta.get("sector"),
                market_cap=meta.get("market_cap"), thesis=meta.get("thesis"),
                max_exit_date=str(risk.add_trading_days(today_et(), hold)) if hold else None))
            db.set_order_status(o["order_id"], "REGISTERED")
            _record_slip("entry", sym, meta.get("trigger") or o["price"], px, today)
            db.set_state(f"filled_entries_{today}", db.get_state(f"filled_entries_{today}", 0) + 1)
            db.log("entry filled", sym, price=px, qty=held[sym]["qty"], stop=stop, setup=setup)
            notify.send(f"✅ BOUGHT {sym} ({setup}) {held[sym]['qty']:g} @ ${px:.2f}\n"
                        f"Stop ${stop:.2f} is live at Schwab. Risk ${(px - stop) * held[sym]['qty']:,.0f}.\n"
                        f"{meta.get('thesis') or ''}")
        dbpos = db.get_positions()

    # 1b. anything held but unmanaged (partial fill, a manual buy) gets a stop right away
    if db.get_state("legacy_done"):
        for sym, h in held.items():
            if sym in dbpos:
                continue
            with db.conn() as c:
                o = c.execute("SELECT * FROM orders WHERE symbol=? AND kind='entry' ORDER BY created DESC LIMIT 1",
                              (sym,)).fetchone()
            meta = json.loads(o["meta"] or "{}") if o else {}
            px = h["avg"] or h["price"]
            d = _held_bars(sym)
            atr_stop = (px - R.LEGACY["stop_atr"] * float(d.iloc[-1].atr)) if d is not None else 0
            stop = round(o["stop"] if o and o["stop"] else max(atr_stop, px * (1 - R.LEGACY["max_stop_pct"])), 2)
            if stop >= h["price"]:
                stop = round(h["price"] * (1 - R.LEGACY["max_stop_pct"]), 2)
            setup = meta.get("setup") or "legacy"
            u = db.get_universe().get(sym, {})
            hold = SETUP_PARAMS.get(setup, {}).get("max_hold_days") or R.LEGACY["max_hold_days"]
            db.upsert_position(dict(
                symbol=sym, setup=setup, entry_date=today, entry_price=px, qty=h["qty"],
                initial_stop=stop, stop=stop, risk_per_share=max(0.01, px - stop), high_water=px,
                sector=meta.get("sector") or u.get("sector"),
                market_cap=meta.get("market_cap") or u.get("market_cap"), thesis=meta.get("thesis"),
                max_exit_date=str(risk.add_trading_days(today_et(), hold))))
            db.log("adopted holding", sym, qty=h["qty"], stop=stop, setup=setup)
            notify.send(f"🛡 {sym}: found {h['qty']:g} sh not under management (partial fill or manual buy). "
                        f"Now managed as {setup}, stop ${stop:.2f}.", critical=True)
        dbpos = db.get_positions()

    # 2. positions that disappeared -> closed trades
    for sym, p in dbpos.items():
        if sym in held:
            continue
        if not live and str(p.get("stop_order_id", "")).startswith("DRY"):
            continue     # dry-run positions mirror real holdings; nothing to close
        stop_o = omap.get(str(p.get("stop_order_id")), {})
        reason, px = "closed", None
        if stop_o.get("status") == "FILLED":
            reason, px = "stop hit", broker.fill_price(stop_o)
        else:
            exits = [o for o in omap.values() if o.get("status") == "FILLED" and any(
                leg.get("instrument", {}).get("symbol") == sym and leg.get("instruction") == "SELL"
                for leg in o.get("orderLegCollection", []))]
            if exits:
                reason, px = "exit", broker.fill_price(exits[0])
        if reason == "stop hit" and px and p.get("stop"):
            _record_slip("stop", sym, p["stop"], px, today)
        px = px or p.get("stop") or p["entry_price"]
        pnl, r = db.record_trade(sym, p["setup"], p["entry_date"], today, p["entry_price"], px,
                                 p["qty"], p["risk_per_share"], reason)
        db.delete_position(sym)
        db.log("position closed", sym, reason=reason, price=px, pnl=pnl, r=r)
        notify.send(f"{'🟢' if pnl >= 0 else '🔴'} CLOSED {sym} ({p['setup']}): {reason} @ ${px:.2f}\n"
                    f"P&L ${pnl:+,.0f} ({r:+.2f}R)" if r is not None else f"CLOSED {sym}: {reason}")

    # 3. quantity drift (partial sells filled) + every position has a live stop
    dbpos = db.get_positions()
    for sym, p in dbpos.items():
        if sym not in held:
            continue
        hq = held[sym]["qty"]
        if not live and int(p["qty"]) < 1:
            db.delete_position(sym)              # dry run sold it all
            continue
        if abs(hq - p["qty"]) > 1e-9 and live:
            if hq < p["qty"]:                    # partial sale filled: book it
                done_ids = set(db.get_state("recorded_exits", []))
                sells = [(k, v) for k, v in omap.items() if k not in done_ids and v.get("status") == "FILLED"
                         and any(l.get("instrument", {}).get("symbol") == sym and l.get("instruction") == "SELL"
                                 for l in v.get("orderLegCollection", []))]
                px = broker.fill_price(sells[0][1]) if sells else None
                if px:
                    db.record_trade(sym, p["setup"], p["entry_date"], today, p["entry_price"], px,
                                    p["qty"] - hq, p["risk_per_share"], "partial")
                    db.set_state("recorded_exits", list(done_ids | {sells[0][0]})[-500:])
            db.update_position(sym, qty=hq)
            p["qty"] = hq
        if p.get("exit_plan") and '"exiting"' in p["exit_plan"]:
            if any(o["symbol"] == sym for o in db.open_orders("exit")) or \
                    any(v.get("status") in broker.WORKING and any(
                        l.get("instrument", {}).get("symbol") == sym and l.get("instruction") == "SELL"
                        for l in v.get("orderLegCollection", [])) for v in omap.values()):
                continue                         # a market exit is in flight: don't re-arm a stop
            db.update_position(sym, exit_plan=None)   # exit failed: fall through and protect it
        if not broker.is_working(p.get("stop_order_id"), omap):
            try:
                oid = broker.place_stop(acct["hash"], sym, int(p["qty"]), p["stop"], setup=p["setup"])
                db.update_position(sym, stop_order_id=oid)
                db.log("stop restored", sym, stop=p["stop"], qty=p["qty"])
                if live:
                    notify.send(f"🛡 {sym}: stop was missing, re-placed at ${p['stop']:.2f}",
                                key=f"restore-{sym}", every=3600, critical=True)
            except Exception as e:
                notify.send(f"⚠️ {sym}: could not place stop: {e}", key=f"stopfail-{sym}", every=1800, critical=True)
        elif live:
            so = omap.get(str(p.get("stop_order_id")), {})
            leg_qty = sum(l.get("quantity", 0) for l in so.get("orderLegCollection", []))
            if leg_qty and abs(leg_qty - p["qty"]) > 1e-9:
                new = broker.replace_stop(acct["hash"], p["stop_order_id"], sym, int(p["qty"]), p["stop"])
                db.update_position(sym, stop_order_id=new)

    # 4. stale entry orders
    for o in db.open_orders("entry"):
        if broker.stale_entry(o):
            broker.cancel(acct["hash"], o["order_id"], o["symbol"])
            watch = [w for w in db.get_watch(today) if w["symbol"] == o["symbol"]]
            if watch:
                tries = watch[0]["meta"].get("attempts", 1)
                db.set_watch_status(today, o["symbol"],
                                    "pending" if tries < R.MAX_ENTRY_ATTEMPTS else "expired")
    return omap


# ── legacy onboarding ────────────────────────────────────────────────────
def legacy_review(acct):
    """One-time: judge each pre-existing holding as a fresh buy; keep with a stop or sell in tranches."""
    dbpos = db.get_positions()
    todo = [s for s in acct["positions"] if s not in dbpos]
    if not todo:
        db.set_state("legacy_done", True)
        return []
    if not db.get_universe():
        data.refresh_universe()
        db.set_state("universe_refreshed", str(today_et()))
    data.update_bars(todo + ["SPY"])
    data.refresh_fundamentals(todo)
    uni = db.get_universe()
    eq = acct["equity"]
    reviews = []
    for sym in todo:
        p = acct["positions"][sym]
        d = _held_bars(sym)
        if d is None:
            reviews.append(dict(symbol=sym, decision="exit", conviction=0,
                                thesis="not enough price history to manage", stop=None, d=None))
            continue
        t = d.iloc[-1]
        price = p["price"]
        stop = risk.legacy_stop(price, float(t.atr), float(t.ma50), float(t.ma200))
        downtrend = price < t.ma50 < t.ma200 and t.ret63 < 0
        metrics = dict(price=round(price, 2), avg_cost=p["avg"], ma50=round(t.ma50, 2),
                       ma200=round(t.ma200, 2) if not math.isnan(t.ma200) else None,
                       ret_3m_pct=round(t.ret63 * 100, 1), off_52w_high_pct=round((1 - price / t.high252) * 100, 1)
                       if not math.isnan(t.high252) else None,
                       atr_pct=round(t.atr / price * 100, 1), proposed_stop=stop,
                       stop_distance_pct=round((price - stop) / price * 100, 1),
                       position_pct=round(p["value"] / eq * 100, 1))
        cache = db.get_state("legacy_verdicts", {}) or {}
        if cache.get(sym, {}).get("date") == str(today_et()):
            v = {k: x for k, x in cache[sym].items() if k != "date"}        # don't pay twice
        else:
            v = analyst.review_legacy(sym, metrics, db.get_fundamentals(sym), data.news(sym),
                                      data.next_earnings(sym))
            cache[sym] = dict(v, date=str(today_et()))
            db.set_state("legacy_verdicts", cache)
        v = dict(v)
        if downtrend and v["decision"] == "keep":
            v["decision"], v["thesis"] = "exit", f"downtrend overrides: {v['thesis']}"
        owner = (db.get_state("legacy_overrides", {}) or {}).get(sym)
        if owner in ("keep", "sell"):
            v["decision"] = "keep" if owner == "keep" else "exit"
            v["thesis"] = f"owner's call ({owner}). " + (v.get("thesis") or "")
            if owner == "keep":
                v["conviction"] = max(v.get("conviction") or 0, 5)
                v["owner_full"] = True
        reviews.append(dict(symbol=sym, stop=stop, metrics=metrics, **v))

    keepers = [dict(symbol=r["symbol"], price=acct["positions"][r["symbol"]]["price"], stop=r["stop"],
                    qty=int(acct["positions"][r["symbol"]]["qty"]), conviction=r.get("conviction") or 0,
                    small=(uni.get(r["symbol"], {}).get("market_cap") or 1e18) < R.SMALLCAP_MAX_MCAP,
                    full=bool(r.get("owner_full")))
               for r in reviews if r["decision"] == "keep" and r.get("stop")]
    alloc, dropped = risk.allocate_legacy(keepers, eq)
    for r in reviews:
        if r["symbol"] in dropped:
            r["decision"] = "exit"
            r["thesis"] = "risk budget went to higher-conviction holdings. " + (r.get("thesis") or "")
    today = today_et()
    for r in reviews:
        sym, p = r["symbol"], acct["positions"][r["symbol"]]
        u = uni.get(sym, {})
        price = p["price"]
        stop = r["stop"] or round(price * (1 - R.LEGACY["max_stop_pct"]), 2)
        base = dict(symbol=sym, entry_date=str(today), entry_price=price, qty=p["qty"],
                    initial_stop=stop, stop=stop, risk_per_share=max(0.01, price - stop),
                    high_water=price, sector=u.get("sector"), market_cap=u.get("market_cap"),
                    thesis=r.get("thesis"), legacy=1,
                    max_exit_date=str(risk.add_trading_days(today, R.LEGACY["max_hold_days"])))
        if r["decision"] == "keep" and r.get("owner_full"):
            base.update(legacy=2, max_exit_date=None)      # owner's keep: only its stop can sell it
        if r["decision"] == "keep":
            trim = max(0, int(p["qty"]) - alloc[sym])
            r["trim"] = trim
            base.update(setup="legacy", qty=p["qty"], exit_plan=None if not trim else
                        json.dumps({"trim": trim, "tranches_left": 1, "next": str(today)}))
        else:
            base.update(setup="legacy_exit", exit_plan=json.dumps(
                {"trim": int(p["qty"]), "tranches_left": R.LEGACY["exit_tranches"], "next": str(today)}))
            if p.get("avg") and price < p["avg"]:          # sold at a loss: 2-month re-entry block
                nb = db.get_state("no_rebuy", {}) or {}
                nb[sym] = str(today + timedelta(days=R.NO_REBUY_AFTER_LOSS_DAYS))
                db.set_state("no_rebuy", nb)
        db.upsert_position(base)
        oid = broker.place_stop(acct["hash"], sym, int(p["qty"]), stop, setup=base["setup"])
        db.update_position(sym, stop_order_id=oid)
        db.log("legacy review", sym, decision=r["decision"], stop=stop, trim=r.get("trim"),
               thesis=r.get("thesis"), red_flags=r.get("red_flags"))
    db.set_state("legacy_done", True)
    lines = ["🧾 DAY-1 REVIEW OF YOUR EXISTING HOLDINGS"]
    for r in sorted(reviews, key=lambda r: r["decision"]):
        p = acct["positions"][r["symbol"]]
        if r["decision"] == "keep":
            extra = f", trim {r['trim']} sh" if r.get("trim") else ""
            lines.append(f"KEEP {r['symbol']}: stop ${r['stop']:.2f}{extra}. {r.get('thesis', '')}")
        else:
            lines.append(f"SELL {r['symbol']} ({p['qty']:g} sh, over {R.LEGACY['exit_tranches']} days, "
                         f"stop ${r['stop'] or 0:.2f} meanwhile). {r.get('thesis', '')}")
    from . import mailer as M
    body = M.table(["Decision", "Ticker", "Shares", "Stop", "Conviction", "Why"], [
        ["<b>KEEP</b>" + (f" (trim {r['trim']})" if r.get("trim") else "") if r["decision"] == "keep" else "SELL",
         M.esc(r["symbol"]), f"{acct['positions'][r['symbol']]['qty']:g}", f"${(r['stop'] or 0):.2f}",
         str(r.get("conviction", "")), M.esc(r.get("thesis", "")) + (
             f"<br><span class='muted'>{M.esc('; '.join(r.get('red_flags') or []))}</span>" if r.get("red_flags") else "")]
        for r in sorted(reviews, key=lambda r: r["decision"])])
    body = (f"<p>Each holding judged as a fresh buy today. Keepers get a stop and share a "
            f"{R.LEGACY['heat_budget']:.0%} risk budget; the rest are sold over {R.LEGACY['exit_tranches']} "
            f"days with a stop meanwhile.</p>" + body)
    M.send("Claudio: day-1 review of your holdings", body, "\n".join(lines))
    k = sum(r["decision"] == "keep" for r in reviews)
    notify.send(f"🧾 Reviewed your {len(reviews)} holdings: keep {k}, sell {len(reviews) - k}. Details by email.")
    return reviews


def run_exit_plans(acct, prices):
    """Sell legacy tranches once per day after the configured time."""
    today = str(today_et())
    for sym, p in db.get_positions().items():
        if not p.get("exit_plan") or sym not in acct["positions"]:
            continue
        plan = json.loads(p["exit_plan"])
        if plan.get("next", today) > today or plan.get("tranches_left", 0) <= 0:
            continue
        try:
            _tranche(acct, sym, p, plan, today)
        except Exception as e:
            on_error(f"tranche {sym}", e)


def _tranche(acct, sym, p, plan, today):
    if True:
        held = acct["positions"][sym]["qty"]
        remaining_trim = min(plan["trim"], held)
        qty = math.ceil(remaining_trim / plan["tranches_left"])
        if qty < 1:
            db.update_position(sym, exit_plan=None)
            return
        keep_qty = int(held - qty)
        # shrink the stop first so stop + sell can never oversell
        if keep_qty >= 1:
            new = broker.replace_stop(acct["hash"], p["stop_order_id"], sym, keep_qty, p["stop"])
        else:
            broker.cancel(acct["hash"], p["stop_order_id"], sym)
            new = None
        if broker.live_enabled():
            time.sleep(2)
        broker.sell_market(acct["hash"], sym, qty, "legacy tranche", held)
        plan.update(trim=remaining_trim - qty, tranches_left=plan["tranches_left"] - 1,
                    next=str(risk.add_trading_days(today_et(), 1)))
        done = plan["tranches_left"] <= 0 or plan["trim"] <= 0
        db.update_position(sym, stop_order_id=new, exit_plan=None if done else json.dumps(plan),
                           qty=keep_qty if not broker.live_enabled() else p["qty"])
        notify.send(f"📤 {sym}: sold {qty} sh ({'last' if done else 'next'} tranche"
                    f"{'' if done else ' tomorrow'}), legacy clean-up.")


# ── intraday ─────────────────────────────────────────────────────────────
def manage_positions(acct, prices, near_close=False):
    today = today_et()
    ctx = db.get_state("bar_ctx", {})
    earn = db.get_state("earnings", {})
    for sym, p in db.get_positions().items():
        if sym not in acct["positions"] or p["setup"] == "legacy_exit" or sym not in prices:
            continue
        if not p.get("stop_order_id") or (p.get("exit_plan") and '"exiting"' in p["exit_plan"]):
            continue
        try:
            _manage_one(acct, sym, p, prices[sym], ctx, earn, today, near_close)
        except Exception as e:
            on_error(f"manage {sym}", e)


def _exit_now(acct, sym, p, held, reason):
    """Cancel the stop, sell at market; if the sell fails, put the stop straight back."""
    if not broker.cancel(acct["hash"], p["stop_order_id"], sym):
        return
    if broker.live_enabled():
        time.sleep(2)
    try:
        broker.sell_market(acct["hash"], sym, held, reason, held)
    except Exception as e:
        oid = broker.place_stop(acct["hash"], sym, int(held), p["stop"], setup=p["setup"])
        db.update_position(sym, stop_order_id=oid)
        raise RuntimeError(f"exit failed, stop restored: {e}")
    db.update_position(sym, stop_order_id=None, exit_plan='{"exiting": true}')
    notify.send(f"📤 SELLING {sym} ({p['setup']}): {reason}")


def _manage_one(acct, sym, p, px, ctx, earn, today, near_close):
    if True:
        c = dict(ctx.get(sym, {}))
        if not near_close:
            c.pop("ma5", None)          # bounce "close above 5-day" only near the close
        # high_water = best completed close since entry; it is what makes a trade a runner
        hw = p.get("high_water") or p["entry_price"]
        if c.get("close") and c["close"] > hw:
            db.update_position(sym, high_water=c["close"])
            p["high_water"] = c["close"]
        trimmed = db.get_state("earnings_trimmed", {}) or {}
        p = dict(p, earnings_trimmed=bool(earn.get(sym)) and trimmed.get(sym) == earn.get(sym))
        ne = date.fromisoformat(earn[sym]) if earn.get(sym) else None
        acts = risk.exit_signals(p, px, c, today, acct["equity"], ne)
        held = acct["positions"][sym]["qty"]
        for kind, arg in acts:
            if kind == "exit":
                is_target = str(arg).startswith("+")
                if not (near_close or is_target):
                    continue
                _exit_now(acct, sym, p, held, arg)
                break
            if kind == "partial":
                keep = int(held - arg)
                new = broker.replace_stop(acct["hash"], p["stop_order_id"], sym, keep, p["stop"])
                if broker.live_enabled():
                    time.sleep(2)
                broker.sell_market(acct["hash"], sym, arg, f"partial +{R.PARTIAL_AT_R:.0f}R", held)
                db.update_position(sym, stop_order_id=new, partial_done=1)
                p["stop_order_id"] = new
                notify.send(f"💰 {sym}: sold {arg} sh at +{R.PARTIAL_AT_R:.0f}R, the rest runs "
                            f"with no time limit")
            if kind == "earnings_trim":
                keep = int(held - arg)
                new = broker.replace_stop(acct["hash"], p["stop_order_id"], sym, keep, p["stop"])
                if broker.live_enabled():
                    time.sleep(2)
                broker.sell_market(acct["hash"], sym, arg, "earnings: half off", held)
                db.update_position(sym, stop_order_id=new)
                p["stop_order_id"] = new
                trimmed[sym] = earn.get(sym)
                db.set_state("earnings_trimmed", trimmed)
                db.log("earnings trim", sym, qty=arg, earnings=earn.get(sym), stop=p["stop"])
                notify.send(f"📉 {sym}: sold {arg} of {int(held)} sh before earnings "
                            f"({earn.get(sym)}); the rest holds with the stop at ${p['stop']:.2f}")
            if kind == "raise_stop" and arg >= p["stop"] * (1 + R.MIN_STOP_RAISE):
                qty = int(held if broker.live_enabled() else p["qty"])
                new = broker.replace_stop(acct["hash"], p["stop_order_id"], sym, qty, arg)
                db.update_position(sym, stop=arg, stop_order_id=new)
                db.log("stop raised", sym, stop=arg)


def try_entries(acct, snap, quotes, now):
    today = str(now.date())
    watch = db.get_watch(today, "pending")
    if not watch or snap.halt != "ok":
        return
    hours = db.get_state("session", {})
    open_dt = datetime.fromisoformat(hours["open"]) if hours.get("open") else None
    close_dt = datetime.fromisoformat(hours["close"]) if hours.get("close") else None
    frac = 1.0
    if open_dt and close_dt:
        frac = max(0.05, min(1.0, (now - open_dt) / (close_dt - open_dt)))
    for w in sorted(watch, key=lambda w: -(w["score"] or 0)):
        if w["setup"] == "bounce":
            continue                           # handled near the close
        q = quotes.get(w["symbol"])
        if not q or not q.get("last") or not q.get("ask"):
            continue
        last, ask = q["last"], q["ask"]
        ext = SETUP_PARAMS[w["setup"]].get("max_extension", 0.03)
        if last < w["trigger"]:
            continue
        if last > w["trigger"] * (1 + ext):
            db.set_watch_status(today, w["symbol"], "missed")
            db.log("entry missed", w["symbol"], reason="gapped/ran past trigger", last=last)
            continue
        vol50 = w["meta"].get("vol50") or 0
        need = (R.MOMENTUM["vol_mult"] if w["setup"] in ("momentum", "catalyst") else 1.0)
        pace = (q.get("volume") or 0) / frac
        if vol50 and pace < need * vol50:
            continue                           # breakout without volume: wait
        limit = round(min(ask * (1 + R.ENTRY_LIMIT_SLIPPAGE), w["trigger"] * (1 + ext)), 2)
        if abs(limit - last) / last > R.MAX_QUOTE_DEVIATION:
            continue
        _enter(acct, snap, w, limit, w["stop"], today)


def _enter(acct, snap, w, limit, stop, today):
    sym = w["symbol"]
    u = db.get_universe().get(sym, {})
    last_out = db.last_stopout(sym)
    cooldown = bool(last_out) and (today_et() - date.fromisoformat(last_out[:10])).days < R.REENTRY_COOLDOWN_DAYS
    cooldown = cooldown or (db.get_state("no_rebuy", {}) or {}).get(sym, "") > str(today_et())
    qty, why = risk.size_trade(snap, sym, w["setup"], limit, stop, u.get("sector", ""),
                               u.get("market_cap"), w["size_mult"], cooldown)
    if qty < 1:
        db.log("entry skipped", sym, reason=why)
        if "already held" in why or "stopped out" in why:
            db.set_watch_status(today, sym, "skipped")
        return False
    thesis = (w["analyst"] or {}).get("thesis", "")
    oid = broker.buy_with_stop(acct["hash"], sym, qty, limit, stop, setup=w["setup"],
                               sector=u.get("sector"), market_cap=u.get("market_cap"),
                               thesis=thesis, trigger=w["trigger"])
    w["meta"]["attempts"] = w["meta"].get("attempts", 0) + 1
    db.save_watch(today, sym, w["setup"], w["trigger"], w["stop"], w["score"], w["size_mult"],
                  w["analyst"], "ordered", **w["meta"])
    snap.pending_entries += 1
    snap.entries_today += 1
    snap.reserved_cash += qty * limit
    notify.send(f"🟡 BUY ORDER {sym} ({w['setup']}): {qty} @ ≤${limit:.2f}, stop ${stop:.2f} "
                f"attached. {why}.{' [DRY RUN]' if oid.startswith('DRY') else ''}\n{thesis}")
    return True


def bounce_entries(acct, snap, quotes, now):
    today = str(now.date())
    for w in db.get_watch(today, "pending"):
        if w["setup"] != "bounce":
            continue
        q = quotes.get(w["symbol"])
        d = _held_bars(w["symbol"])
        if not q or not q.get("last") or not q.get("ask") or d is None:
            continue
        ok, stop, why = setups.bounce_confirm(d, q["last"])
        if not ok:
            db.set_watch_status(today, w["symbol"], "no_signal")
            db.log("bounce not confirmed", w["symbol"], reason=why)
            continue
        limit = round(q["ask"] * (1 + R.ENTRY_LIMIT_SLIPPAGE), 2)
        if stop >= limit:
            continue
        _enter(acct, snap, w, limit, stop, today)


def _record_slip(kind, sym, ref, fill, day):
    """What we aimed at vs what we got, in basis points. Negative = worse for us.

    Entries: fill above the trigger costs money. Stops: filling below the stop is the
    gap cost, the one risk the stop order cannot protect against. Both assumptions sit
    under every backtest number, so they get measured rather than assumed.
    """
    try:
        ref, fill = float(ref or 0), float(fill or 0)
        if ref <= 0 or fill <= 0:
            return
        bps = (fill - ref) / ref * 10_000
        if kind == "entry":
            bps = -bps                       # paying more than the trigger is the cost
        log = db.get_state("slippage", {}) or {}
        rows = (log.get(kind) or []) + [dict(symbol=sym, date=day, ref=round(ref, 2),
                                             fill=round(fill, 2), bps=round(bps, 1))]
        log[kind] = rows[-200:]
        db.set_state("slippage", log)
        db.log(f"{kind} slippage", sym, ref=round(ref, 2), fill=round(fill, 2), bps=round(bps, 1))
    except Exception as e:
        db.log("slippage_error", sym, error=str(e)[:150])


def _record_shadow(day, shown, pm):
    """Write down every candidate the AI saw and what it decided, so the picking can be
    scored later against the trades it passed on. Bookkeeping only: places nothing."""
    taken = {e["symbol"]: e for e in pm.get("entries", [])}
    vetoed = {v["symbol"]: v.get("reason", "") for v in pm.get("vetoes", [])}
    for rank, c in enumerate(shown, 1):
        sym = c["symbol"]
        if sym in taken:
            decision, size, why = "taken", taken[sym].get("size"), taken[sym].get("thesis", "")
        elif sym in vetoed:
            decision, size, why = "vetoed", None, vetoed[sym]
        else:
            decision, size, why = "passed", None, ""
        try:
            db.save_shadow(day, sym, c["setup"], decision, rank, c["_c"].get("score"),
                           c["trigger"], c["stop"], size, why)
        except Exception as e:
            db.log("shadow_error", sym, error=str(e)[:200])


# ── nightly ──────────────────────────────────────────────────────────────
def nightly(acct, progress=print, watch_date=None):
    """After the close: refresh data, update trailing context, build tomorrow's watchlist."""
    t0 = datetime.now()
    uni = db.get_universe()
    last_uni = db.get_state("universe_refreshed")
    if not uni or not last_uni or (today_et() - date.fromisoformat(last_uni)).days >= 6:
        n = data.refresh_universe()
        db.set_state("universe_refreshed", str(today_et()))
        uni = db.get_universe()
        progress(f"universe: {n} symbols")
    held = list(acct["positions"])
    symbols = sorted(set(uni) | set(held) | {"SPY"})
    data.update_bars(symbols, progress=lambda n: progress(f"bars {n}/{len(symbols)}"))
    last_f = db.get_state("fundamentals_refreshed")
    if not last_f or (today_et() - date.fromisoformat(last_f)).days >= 6:
        data.refresh_fundamentals([s for s in uni if uni[s]["sector"] != "ETF"])
        db.set_state("fundamentals_refreshed", str(today_et()))

    # regime
    spy = indicators.enrich(db.load_bars("SPY", 300))
    vix = (data.quotes(["$VIX"]).get("$VIX") or {}).get("last")
    s = spy.iloc[-1]
    reg = risk.regime(s.close, s.ma50, s.ma200, vix)
    db.set_state("regime", reg)
    db.set_state("vix", vix)

    # enrich everything once
    frames, rets = {}, {}
    for sym in symbols:
        d = db.load_bars(sym, 300)
        if len(d) < R.MIN_HISTORY_BARS:
            continue
        d = indicators.enrich(d)
        t = d.iloc[-1]
        if not (t.close >= R.MIN_PRICE and t.dollar_vol50 >= R.MIN_DOLLAR_VOLUME):
            if sym not in held:
                continue
        frames[sym] = d
        if not math.isnan(t.ret63):
            rets[sym] = t.ret63
    ranked = sorted(rets, key=rets.get)
    rs_pct = {sym: i / max(1, len(ranked) - 1) for i, sym in enumerate(ranked)}

    # trailing context + earnings for holdings
    ctx, earn = {}, {}
    for sym in held:
        d = frames.get(sym) if sym in frames else _held_bars(sym)
        if d is not None:
            ctx[sym] = _bar_ctx(d)
        ne = data.next_earnings(sym)
        if ne:
            earn[sym] = str(ne)
    db.set_state("bar_ctx", ctx)
    db.set_state("earnings", earn)
    # A missing earnings date looks exactly like "no earnings coming" to the exit rules,
    # so the positions it matters for are named out loud instead of failing silently.
    dbpos = db.get_positions()
    blind = sorted(sym for sym, p in dbpos.items()
                   if sym not in earn and p["setup"] in (
                       set(R.EXIT_BEFORE_EARNINGS) | set(R.EARNINGS_HOLD_SETUPS)))
    db.set_state("earnings_unknown", blind)
    if blind:
        db.log("earnings coverage gap", None, symbols=blind)

    # candidates
    funds = db.get_fundamentals()
    sector_pe = {}
    for sym, f in funds.items():
        sec = uni.get(sym, {}).get("sector")
        if sec and f.get("peRatio") and 0 < f["peRatio"] < 200:
            sector_pe.setdefault(sec, []).append(f["peRatio"])
    sector_pe = {k: sorted(v)[len(v) // 2] for k, v in sector_pe.items() if len(v) >= 10}

    cands = []
    blocked = {k for k, v in (db.get_state("no_rebuy", {}) or {}).items() if v > str(today_et())}
    for sym, d in frames.items():
        if sym in held or sym in blocked:
            continue
        u = uni.get(sym, {})
        is_etf = u.get("sector") == "ETF"
        c = setups.scan_momentum(sym, d, rs_pct.get(sym))
        if c:
            cands.append(c)
        c = setups.scan_bounce(sym, d)
        if c:
            cands.append(c)
        if not is_etf:
            gaps = (d["open"] / d["close"].shift(1) - 1).tail(3)
            vmult = (d["volume"] / d["vol50"]).tail(3)
            if ((gaps >= R.CATALYST["min_gap"]) & (vmult >= R.CATALYST["vol_mult"])).any():
                c = setups.scan_catalyst(sym, d, data.earnings_dates(sym, back=7, fwd=0))
                if c:
                    cands.append(c)
            f = funds.get(sym)
            if f:
                c = setups.scan_value(sym, d, f, sector_pe.get(u.get("sector")))
                if c:
                    cands.append(c)
    for c in cands:
        u = uni.get(c["symbol"], {})
        c.update(sector=u.get("sector"), industry=u.get("industry"), market_cap=u.get("market_cap"))

    # candidates the PM gets to see: enabled + regime-allowed, best few per setup
    allowed = [c for c in cands if R.SETUPS_ENABLED.get(c["setup"], True)
               and risk.regime_mult(reg, c["setup"]) > 0]
    per_setup = {}
    for c in sorted(allowed, key=lambda c: -c["score"]):
        if len(per_setup.setdefault(c["setup"], [])) < R.PM_MAX_PER_SETUP:
            per_setup[c["setup"]].append(c)
    snap = snapshot(acct)
    room = snap.equity * R.MAX_OPEN_RISK - snap.open_risk()
    capacity = max(0, min(R.MAX_POSITIONS - snap.count(), R.MAX_NEW_ENTRIES_PER_DAY,
                          int(room / (snap.equity * R.RISK_PER_TRADE * 0.5)) if room > 0 else 0))
    shown = []
    if capacity:
        for c in sorted((c for v in per_setup.values() for c in v), key=lambda c: -c["score"]):
            if len(shown) >= R.PM_MAX_CANDIDATES:
                break
            ne = data.next_earnings(c["symbol"])
            hold = SETUP_PARAMS[c["setup"]].get("max_hold_days", 15)
            if ne and c["setup"] in R.EXIT_BEFORE_EARNINGS and (ne - today_et()).days <= hold * 1.5:
                db.log("candidate skipped", c["symbol"], reason=f"earnings {ne} inside hold window")
                continue
            f = funds.get(c["symbol"], {})
            shown.append(dict(
                symbol=c["symbol"], setup=c["setup"], note=c.get("note"), trigger=c["trigger"],
                stop=c["stop"], stop_pct=round((c["trigger"] - c["stop"]) / c["trigger"] * 100, 1),
                sector=c.get("sector"), industry=c.get("industry"), market_cap=c.get("market_cap"),
                next_earnings=str(ne) if ne else "unknown",
                fundamentals={k: f.get(k) for k in ("peRatio", "pegRatio", "revChangeTTM",
                                                    "epsChangePercentTTM", "grossMarginTTM",
                                                    "totalDebtToEquity", "shortIntToFloat")
                              if f.get(k) is not None},
                headlines=[n["headline"] for n in data.news(c["symbol"], days=5, limit=4)],
                _c=c))

    next_day = watch_date or _next_session_date()
    book = _book_for_pm(acct, snap, frames)
    pm = None
    if book or shown:
        payload = dict(
            date=str(today_et()), next_session=str(next_day),
            market=dict(regime=reg, vix=vix, spy_close=round(float(s.close), 2),
                        spy_vs_ma50_pct=round((s.close / s.ma50 - 1) * 100, 1),
                        spy_vs_ma200_pct=round((s.close / s.ma200 - 1) * 100, 1),
                        spy_5d_pct=round((s.close / spy["close"].iloc[-6] - 1) * 100, 1)),
            account=dict(equity=round(acct["equity"], 2), settled_cash=round(acct["settled_cash"], 2),
                         open_risk_pct=round(snap.open_risk() / snap.equity * 100, 2),
                         open_risk_cap_pct=R.MAX_OPEN_RISK * 100, positions=snap.count(),
                         max_positions=R.MAX_POSITIONS, capacity=capacity,
                         sector_exposure_pct=_sector_exposure(snap)),
            holdings=book,
            candidates=[{k: v for k, v in c.items() if k != "_c"} for c in shown],
            recent_closed_trades=[dict(symbol=t["symbol"], setup=t["setup"], r=round(t["r_multiple"] or 0, 2),
                                       reason=t["reason"], exit_date=t["exit_date"]) for t in db.trades()[-6:]],
            previous_memos=(db.get_state("pm_memos", []) or [])[-R.PM_MEMO_HISTORY:])
        pm = analyst.pm_review(payload)
    approved = []
    if pm:
        _record_shadow(str(today_et()), shown, pm)
        by_sym = {c["symbol"]: c["_c"] for c in shown}
        for rank, e in enumerate(pm["entries"]):
            c = by_sym[e["symbol"]]
            db.save_watch(str(next_day), c["symbol"], c["setup"], c["trigger"], c["stop"],
                          1000 - rank, e["size_mult"], e, "pending", vol50=c.get("vol50"),
                          note=c.get("note"), rank=rank + 1)
            approved.append((c, e))
        _apply_pm_holdings(acct, pm["holdings"], next_day)
        memos = (db.get_state("pm_memos", []) or []) + [dict(date=str(today_et()), memo=pm["memo"])]
        db.set_state("pm_memos", memos[-30:])
        db.log("pm", None, memo=pm["memo"], entries=[e["symbol"] for e in pm["entries"]],
               vetoes=pm["vetoes"], holdings=[h for h in pm["holdings"] if h["action"] != "hold"])
    elif (book or shown) and analyst.key_status().get("ok") is not False:   # a dead key has its own alert
        notify.send("⚠️ Portfolio manager call failed tonight: no new entries tomorrow. "
                    "Holdings keep their stops and rules.", key="pmfail", every=3600, critical=True)

    # equity bookkeeping
    db.set_state("eod_equity", acct["equity"])
    peak = max(db.get_state("peak_equity", 0) or 0, acct["equity"])
    db.set_state("peak_equity", peak)
    curve = db.get_state("equity_curve", [])
    curve = [x for x in curve if x[0] != str(today_et())] + [[str(today_et()), acct["equity"]]]
    db.set_state("equity_curve", curve[-400:])
    db.set_state("regime_history", ((db.get_state("regime_history", []) or []) +
                                    [[str(today_et()), reg, vix]])[-60:])
    db.set_state("nightly_done", str(today_et()))
    return dict(regime=reg, vix=vix, scanned=len(frames), candidates=len(cands),
                shown=[(c["symbol"], c["setup"]) for c in shown], pm=pm, approved=approved,
                capacity=capacity, minutes=round((datetime.now() - t0).seconds / 60, 1),
                next_day=str(next_day))


def _sector_exposure(snap):
    out = {}
    for h in snap.holdings:
        out[h.sector or "?"] = out.get(h.sector or "?", 0) + h.value
    return {k: round(v / snap.equity * 100, 1) for k, v in sorted(out.items(), key=lambda x: -x[1])}


def _book_for_pm(acct, snap, frames):
    dbpos = db.get_positions()
    earn = db.get_state("earnings", {})
    out = []
    for h in snap.holdings:
        p = dbpos.get(h.symbol)
        if not p:
            continue
        d = frames.get(h.symbol)
        if d is None:
            d = _held_bars(h.symbol)
        trend = {}
        if d is not None:
            t = d.iloc[-1]
            num = lambda x: None if x != x else round(float(x), 2)
            trend = dict(close=num(t.close), ma50=num(t.ma50), ma200=num(t.ma200),
                         ret_5d_pct=num((t.close / d["close"].iloc[-6] - 1) * 100) if len(d) > 6 else None,
                         low10=num(t.low10))
        rps = p.get("risk_per_share") or 0
        out.append(dict(
            symbol=h.symbol, setup=p["setup"], entry_date=p["entry_date"], entry=round(p["entry_price"], 2),
            price=round(h.price, 2), stop=p["stop"], r_now=round((h.price - p["entry_price"]) / rps, 2) if rps else None,
            pct_of_equity=round(h.value / snap.equity * 100, 1), sector=h.sector,
            owner_kept=p.get("legacy") == 2,
            thesis=p.get("thesis"), next_earnings=earn.get(h.symbol, "unknown"), trend=trend,
            headlines=[n["headline"] for n in data.news(h.symbol, days=3, limit=3)]))
    return out


def _apply_pm_holdings(acct, actions, next_day):
    exits = {}
    for a in actions:
        sym = a["symbol"]
        p = db.get_positions().get(sym)
        if not p:
            continue
        if a["action"] == "tighten" and p.get("stop_order_id"):
            try:
                qty = int(acct["positions"][sym]["qty"] if broker.live_enabled() else p["qty"])
                new = broker.replace_stop(acct["hash"], p["stop_order_id"], sym, qty, a["new_stop"])
                db.update_position(sym, stop=a["new_stop"], stop_order_id=new)
                db.log("pm tighten", sym, stop=a["new_stop"], reason=a["reason"])
            except Exception as e:
                on_error(f"pm tighten {sym}", e)
        elif a["action"] == "exit":
            exits[sym] = a["reason"]
    if exits:
        db.set_state("pm_exits", dict(date=str(next_day), symbols=exits))


def run_pm_exits(acct):
    """Sell holdings the PM (nightly or pre-market) marked for exit at today's first tick."""
    plan = db.get_state("pm_exits") or {}
    if plan.get("date") != str(today_et()) or not plan.get("symbols"):
        return
    for sym, reason in plan["symbols"].items():
        p = db.get_positions().get(sym)
        if not p or sym not in acct["positions"] or not p.get("stop_order_id"):
            continue
        try:
            _exit_now(acct, sym, p, acct["positions"][sym]["qty"], f"PM: {reason}")
        except Exception as e:
            on_error(f"pm exit {sym}", e)
    db.set_state("pm_exits", {})


def premarket_check():
    """Before the open: one AI call if overnight news touches a holding or a planned entry."""
    today = str(today_et())
    holdings = list(db.get_positions())
    watch = [w["symbol"] for w in db.get_watch(today, "pending")]
    news = {}
    for sym in holdings + watch:
        fresh = [n for n in data.news(sym, days=1, limit=5) if n["date"] >= str(today_et() - timedelta(days=1))]
        if fresh:
            news[sym] = fresh
    if not news:
        return None
    memo = (db.get_state("pm_memos", []) or [{}])[-1]
    v = analyst.premarket(dict(date=today, holdings=holdings, watchlist=watch, overnight_news=news,
                               last_memo=memo, planned_entries=[
                                   dict(symbol=w["symbol"], setup=w["setup"], thesis=(w["analyst"] or {}).get("thesis"))
                                   for w in db.get_watch(today, "pending")]))
    if not v:
        return None
    for x in v["cancel_entries"]:
        db.set_watch_status(today, x["symbol"], "vetoed")
        db.log("premarket cancel", x["symbol"], reason=x["reason"])
    if v["exit_at_open"]:
        plan = db.get_state("pm_exits") or {}
        syms = plan.get("symbols", {}) if plan.get("date") == today else {}
        syms.update({x["symbol"]: x["reason"] for x in v["exit_at_open"]})
        db.set_state("pm_exits", dict(date=today, symbols=syms))
    if v["exit_at_open"] or v["cancel_entries"]:
        lines = ["🌅 Pre-market check"]
        lines += [f"SELL at open {x['symbol']}: {x['reason']}" for x in v["exit_at_open"]]
        lines += [f"Cancelled entry {x['symbol']}: {x['reason']}" for x in v["cancel_entries"]]
        notify.send("\n".join(lines), critical=True)
    db.log("premarket", None, **v)
    return v


def _next_session_date():
    d = today_et()
    for _ in range(10):
        d += timedelta(days=1)
        try:
            if data.market_hours(d):
                return d
        except Exception:
            if d.weekday() < 5:
                return d
    return d


def recap(acct, res):
    """Daily email after the close (Telegram gets one line)."""
    from . import mailer as M
    snap = snapshot(acct)
    publish_status(acct, snap)
    st = db.get_state("status")
    eq = acct["equity"]
    start = db.get_state("day_start_equity") or eq
    day_pct = (eq / start - 1) * 100
    today = str(today_et())
    closed = db.trades(today)
    pm = res.get("pm") or {}
    live = broker.live_enabled()
    subject = f"Claudio {today}: ${eq:,.0f} ({day_pct:+.2f}%)" + ("" if live else " [dry run]")

    kpis = "".join(f"<div class='kpi'><span class='muted'>{k}</span><b>{v}</b></div>" for k, v in [
        ("Equity", f"${eq:,.0f}"), ("Today", M.signed(day_pct, "{:+.2f}", "%")),
        ("Peak", f"${db.get_state('peak_equity') or eq:,.0f}"), ("Settled cash", f"${acct['settled_cash']:,.0f}"),
        ("Open risk", f"{st['heat']:.1f}% / {R.MAX_OPEN_RISK * 100:.1f}%"),
        ("Regime", f"{res['regime']} · VIX {res['vix'] or '?'}")])
    body = f"<div class='kpis'>{kpis}</div>"
    if pm.get("memo"):
        body += f"<h2>Portfolio manager</h2><div class='memo'>{M.esc(pm['memo'])}</div>"
        if pm.get("themes"):
            body += f"<p class='muted'>Watching: {M.esc(', '.join(pm['themes']))}</p>"
    body += "<h2>Positions</h2>"
    dbpos = db.get_positions()
    if st["positions"]:
        body += M.table(["Ticker", "Setup", "Qty", "Price", "Stop", "R", "% equity", "Thesis"], [
            [M.esc(p["symbol"]), M.esc(p["setup"]), f"{p['qty']:g}", f"${p['price']:.2f}",
             f"${p['stop']:.2f}" if p["stop"] else "<b>none</b>", M.signed(p["r"]), f"{p['pct']:.1f}%",
             M.esc((dbpos.get(p["symbol"], {}).get("thesis") or "")[:140])] for p in st["positions"]])
    else:
        body += "<p>All cash.</p>"
    acts = [a for a in (pm.get("holdings") or []) if a["action"] != "hold"]
    if acts:
        body += "<h2>Changes to holdings</h2>" + M.table(["Ticker", "Action", "Why"], [
            [M.esc(a["symbol"]), M.esc(a["action"] + (f" → ${a['new_stop']:.2f}" if a.get("new_stop") else "")),
             M.esc(a["reason"])] for a in acts])
    if closed:
        body += "<h2>Closed today</h2>" + M.table(["Ticker", "Setup", "Exit", "P&L", "R", "Reason"], [
            [M.esc(t["symbol"]), M.esc(t["setup"]), f"${t['exit_price']:.2f}", M.signed(t["pnl"], "${:+,.0f}"),
             M.signed(t["r_multiple"]), M.esc(t["reason"])] for t in closed])
    todays = [j for j in db.recent_journal(limit=200) if j["ts"][:10] >= today and j["action"] in (
        "entry filled", "position closed", "stop raised", "pm tighten", "premarket cancel", "adopted holding",
        "entry missed", "DRY entry", "placed entry")]
    if todays:
        body += "<h2>Activity</h2>" + M.table(["Time (UTC)", "Ticker", "What"], [
            [j["ts"][11:16], M.esc(j["symbol"] or ""), M.esc(j["action"])] for j in reversed(todays[:25])])
    body += f"<h2>Plan for {res['next_day']}</h2>"
    if res["approved"]:
        body += M.table(["#", "Ticker", "Setup", "Buy above", "Stop", "Size", "Thesis", "Wrong if"], [
            [str(i + 1), M.esc(c["symbol"]), M.esc(c["setup"]), f"${c['trigger']:.2f}", f"${c['stop']:.2f}",
             "half" if e["size_mult"] < 1 else "full", M.esc(e["thesis"]), M.esc(e.get("invalidation", ""))]
            for i, (c, e) in enumerate(res["approved"])])
    elif not res["capacity"]:
        body += "<p>No new trades: positions or risk budget are full.</p>"
    else:
        body += "<p>Nothing good enough. Sitting tight is a position too.</p>"
    if pm.get("vetoes"):
        body += "<p class='muted'>Passed on: " + M.esc("; ".join(f"{v['symbol']} ({v['reason']})"
                                                            for v in pm["vetoes"])) + "</p>"
    body += (f"<p class='muted'>Scanned {res['scanned']} stocks, {res['candidates']} setups, "
             f"{len(res['shown'])} reviewed. AI cost to date ${analyst.cost_to_date():.2f}. "
             f"{'LIVE trading' if live else 'Dry run: no orders sent'}.</p>")
    bench = metrics.summary_line()
    if bench:
        body += f"<p class='muted'>{M.esc(bench)}</p>"
    blind = db.get_state("earnings_unknown", []) or []
    if blind:
        body += ("<p class='muted'>⚠️ No earnings date found for " + M.esc(", ".join(blind))
                 + ". The rule that keeps short-term trades out of earnings can't protect "
                 "these, so treat them as exposed to a report.</p>")
    ks = analyst.key_status()
    if ks.get("ok") is False:
        body += (f"<p><b>⚠️ Claude API key not working</b> ({M.esc(analyst.KIND_TEXT.get(ks.get('kind'), ''))}): "
                 f"no new trades until it's replaced. {M.esc(analyst.KEY_HELP)}</p>")

    text = [subject, "", pm.get("memo", ""), "",
            "Positions: " + (", ".join(f"{p['symbol']} {p['r']:+.1f}R" if p["r"] is not None else p["symbol"]
                                       for p in st["positions"]) or "none"),
            f"Plan {res['next_day']}: " + (", ".join(f"{c['symbol']} ({c['setup']}) >{c['trigger']:.2f} stop "
                                                     f"{c['stop']:.2f}" for c, e in res["approved"]) or "no entries")]
    sent = M.send(subject, body, "\n".join(text))
    if sent:
        notify.send(f"📧 Daily report sent: ${eq:,.0f} ({day_pct:+.2f}%), "
                    f"{len(res['approved'])} entr{'y' if len(res['approved']) == 1 else 'ies'} planned.")


def _setup_stats(ts):
    rs = [t["r_multiple"] for t in ts if t["r_multiple"] is not None]
    if not rs:
        return None
    wins = [r for r in rs if r > 0]
    return dict(trades=len(rs), win_rate=round(len(wins) / len(rs), 2), avg_r=round(sum(rs) / len(rs), 2),
                best_r=round(max(rs), 2), worst_r=round(min(rs), 2), pnl=round(sum(t["pnl"] for t in ts), 2))


def weekly_report():
    """Saturday: scorecard + AI strategist memo + tuning proposals (owner approves in Telegram)."""
    from . import mailer as M, tuning
    tuning.load()
    all_t = db.trades()
    since = str(today_et() - timedelta(days=7))
    since4 = str(today_et() - timedelta(days=28))
    week = [t for t in all_t if t["exit_date"] >= since]
    setups_ = ("momentum", "catalyst", "value", "bounce", "legacy", "legacy_exit")
    stats = {k: dict(all_time=_setup_stats([t for t in all_t if t["setup"] == k]),
                     last_4w=_setup_stats([t for t in all_t if t["setup"] == k and t["exit_date"] >= since4]))
             for k in setups_}
    stats = {k: v for k, v in stats.items() if v["all_time"]}
    curve = db.get_state("equity_curve", [])
    with db.conn() as c:
        wl = [dict(r) for r in c.execute("SELECT status, COUNT(*) n FROM watchlist WHERE date>=? GROUP BY status",
                                         (since,))]
    res = analyst.weekly(dict(
        week_ending=str(today_et()), trades_last_30d=[{k: t[k] for k in ("symbol", "setup", "entry_date", "exit_date",
                                                                          "r_multiple", "pnl", "reason")}
                                                       for t in all_t if t["exit_date"] >= str(today_et() - timedelta(days=30))],
        stats_by_setup=stats, watchlist_outcomes_this_week=wl,
        pm_memos_this_week=[m for m in (db.get_state("pm_memos", []) or []) if m["date"] >= since],
        equity_curve=curve[-30:], regime_history=(db.get_state("regime_history", []) or [])[-10:],
        tunable=tuning.current())) if all_t or curve else None

    try:
        metrics.score_pending(today_et())
    except Exception as e:
        db.log("metrics_error", None, error=str(e)[:200])
    props = (res or {}).get("proposals", [])
    stored = db.get_state("proposals", {}) or {}
    next_id = max([int(k) for k in stored] + [0]) + 1
    for p in props:
        p["id"] = next_id
        stored[str(next_id)] = dict(p, status="open", date=str(today_et()))
        next_id += 1
    db.set_state("proposals", stored)

    wk = [x for x in curve if x[0] >= since]
    start_eq, end_eq = (wk[0][1], curve[-1][1]) if wk else (None, None)
    ws = _setup_stats(week)
    subject = f"Claudio weekly review, {today_et()}"
    body = ""
    win_txt = f"{ws['win_rate']:.0%}" if ws else "–"
    if start_eq:
        body += (f"<div class='kpis'><div class='kpi'><span class='muted'>Week</span><b>"
                 f"{M.signed((end_eq / start_eq - 1) * 100, '{:+.2f}', '%')}</b></div>"
                 f"<div class='kpi'><span class='muted'>Equity</span><b>${end_eq:,.0f}</b></div>"
                 f"<div class='kpi'><span class='muted'>Trades</span><b>{ws['trades'] if ws else 0}</b></div>"
                 f"<div class='kpi'><span class='muted'>Win rate</span><b>{win_txt}</b></div></div>")
    if res and res.get("memo"):
        body += f"<h2>Strategist</h2><div class='memo'>{M.esc(res['memo'])}</div>"
    if stats:
        body += "<h2>By setup (all time)</h2>" + M.table(["Setup", "Trades", "Win", "Avg R", "Best", "Worst", "P&L"], [
            [k, str(v["all_time"]["trades"]), f"{v['all_time']['win_rate']:.0%}", M.signed(v["all_time"]["avg_r"]),
             f"{v['all_time']['best_r']:+.1f}", f"{v['all_time']['worst_r']:+.1f}",
             M.signed(v["all_time"]["pnl"], "${:+,.0f}")] for k, v in stats.items()])
    b = metrics.benchmark()
    if b:
        body += ("<h2>Against the index</h2><p>Since " + M.esc(b["since"]) + ": account "
                 + M.signed(b["me_pct"], "{:+.2f}", "%") + ", SPY "
                 + M.signed(b["spy_pct"], "{:+.2f}", "%") + " &mdash; "
                 + M.signed(b["diff_pct"], "{:+.2f}", " points") + " vs buy-and-hold.</p>")
    ex = metrics.execution_note()
    if ex:
        body += f"<h2>Execution</h2><p>{M.esc(ex)}</p>"
    sc = metrics.scorecard()
    if sc["all"].get("n"):
        a, rc = sc["all"], sc["recent"]
        body += "<h2>Scorecard</h2>" + M.table(
            ["", "Trades", "Win", "Expectancy", "Avg win", "Avg loss", "Profit factor", "P&L"],
            [[lbl, str(x["n"]), f"{x.get('win_pct', 0):.0f}%", M.signed(x.get("expectancy", 0), "{:+.3f}", "R"),
              M.signed(x.get("avg_win", 0), "{:+.2f}", "R"), M.signed(x.get("avg_loss", 0), "{:+.2f}", "R"),
              str(x.get("pf") or "-"), M.signed(x.get("pnl", 0), "${:+,.0f}")]
             for lbl, x in (("All time", a), ("Last 20", rc)) if x.get("n")])
        if not sc["enough_data"]:
            body += (f"<p class='muted'>{a['n']} trades so far. Treat any of this as a signal "
                     "only past about 100 trades.</p>")
        if sc["flagged"]:
            body += ("<p class='muted'>Losing money after " + str(R.SETUP_REVIEW_MIN_TRADES)
                     + "+ trades: " + M.esc(", ".join(sc["flagged"])) + ".</p>")
    at = metrics.attribution()
    if at["scored"]:
        g = at["groups"]
        body += "<h2>Is the AI's picking helping?</h2>" + M.table(
            ["The AI...", "Candidates", "Would have traded", "Win", "Expectancy"],
            [[lbl, str(g[k]["n"]), str(g[k].get("traded", 0)), f"{g[k].get('win_pct', 0):.0f}%",
              M.signed(g[k].get("expectancy", 0), "{:+.3f}", "R")]
             for k, lbl in (("taken", "bought"), ("vetoed", "vetoed"), ("passed", "passed over"))
             if g[k]["n"]])
        body += (f"<p>{M.esc(at['verdict'])}"
                 + (f" (gap: {at['edge_r']:+.3f}R per trade)" if at["edge_r"] is not None else "")
                 + f". {at['scored']} candidates scored so far; a verdict needs "
                 f"{at['min_for_verdict']} in each group.</p>")
    if props:
        body += "<h2>Proposed rule changes</h2>" + M.table(["#", "Change", "Why"], [
            [str(p["id"]), M.esc(f"{p['key']}: {p['current']} → {p['value']}"), M.esc(p["why"])] for p in props])
        body += ("<p>To apply one: Telegram <b>/approve N</b>, or on the host machine "
                 "<code>venv312/bin/python -m engine.admin approve N</code> (reject the same way).</p>")
    ov = db.get_state("overrides", {}) or {}
    if ov:
        body += "<p class='muted'>Active tuning: " + M.esc(", ".join(f"{k}={v}" for k, v in ov.items())) + "</p>"
    text = subject + "\n\n" + ((res or {}).get("memo") or "No trades yet.") + "\n\n" + "\n".join(
        f"#{p['id']} {p['key']}: {p['current']} -> {p['value']} ({p['why']})" for p in props)
    M.send(subject, body or "<p>No trades yet.</p>", text)
    if props:
        notify.send("🧠 Weekly review is in your inbox. Proposed changes:\n" + "\n".join(
            f"#{p['id']} {p['key']}: {p['current']} → {p['value']}\n   {p['why']}" for p in props) +
            "\n\n/approve N or /reject N")


def on_error(where, e):
    db.log("error", None, where=where, error=notify.redact(str(e))[:500],
           tb=notify.redact(traceback.format_exc())[-1500:])
    notify.send(f"⚠️ Claudio engine error in {where}: {notify.redact(str(e))[:300]}",
                key=f"err-{where}", every=1800, critical=True)
