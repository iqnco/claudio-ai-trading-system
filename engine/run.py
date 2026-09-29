"""Claudio engine main loop (launchd keeps it alive).

    venv312/bin/python -u -m engine.run

Schedule (US Eastern, from Schwab's market calendar, so holidays and early
closes are handled):
  open-35m   pre-market AI check (only if overnight news hits a holding or watchlist name)
  open-15m   morning: set day-start equity, one-time review of old holdings
  9:45-15:45 every 5 min: reconcile, kill switches, manage stops, entries
  close-25m  near-close pass: bounce entries, time / earnings exits
  close+15m  nightly: data refresh, scan, AI portfolio manager, tomorrow's plan, email
  Sat 10:00  weekly review: scorecard + AI strategist + proposed tuning (email)
  daily 8:30 1-token ping of the Claude API key (every 30 min while it's failing); email if it dies
"""
import time
from datetime import datetime, time as dtime, timedelta

from . import analyst, broker, core, data, db, notify, tuning
from . import rules as R

KEY_CHECK_ET = dtime(8, 30)
KEY_RECHECK_S = 1800


def _session(day):
    s = db.get_state("session")
    if s and s.get("day") == str(day):
        return s
    hrs = data.market_hours(day)
    s = {"day": str(day), "open": hrs[0].isoformat() if hrs else None,
         "close": hrs[1].isoformat() if hrs else None}
    db.set_state("session", s)
    return s


def _done(tag, day):
    return db.get_state(f"done_{tag}") == str(day)


def _mark(tag, day):
    db.set_state(f"done_{tag}", str(day))


def _cancel_entries(acct):
    for o in db.open_orders("entry"):
        broker.cancel(acct["hash"], o["order_id"], o["symbol"])


def morning(now):
    acct = data.account()
    db.set_state("last_session_day", str(now.date()))
    db.set_state("day_start_equity", db.get_state("eod_equity") or acct["equity"])
    if db.get_state("peak_equity") is None:
        db.set_state("peak_equity", acct["equity"])
    if not db.get_state("legacy_done"):
        core.legacy_review(acct)
    core.publish_status(acct, core.snapshot(acct))


def tick(now, near_close):
    tuning.load()
    today = str(now.date())
    acct = data.account()
    core.reconcile(acct)
    watch = [w["symbol"] for w in db.get_watch(today, "pending")]
    q = data.quotes(list(acct["positions"]) + watch) if (acct["positions"] or watch) else {}
    prices = {s: v["last"] for s, v in q.items() if v.get("last")}
    snap = core.snapshot(acct, prices)

    if snap.halt == "full_halt" and not db.get_state("halt_full"):
        db.set_state("halt_full", True)
        _cancel_entries(acct)
        notify.send(f"🛑 KILL SWITCH: equity ${acct['equity']:,.0f} is {R.TOTAL_DRAWDOWN_HALT:.0%} below "
                    f"peak ${db.get_state('peak_equity'):,.0f}. No new trades until you send /resume. "
                    f"Existing stops stay live.", critical=True)
    elif snap.halt == "day_halt" and not db.get_state(f"day_halt_{today}"):
        db.set_state(f"day_halt_{today}", True)
        _cancel_entries(acct)
        notify.send(f"⏸ Daily loss limit: down {R.DAILY_LOSS_HALT:.0%} today. No new trades until "
                    f"tomorrow. Stops stay live.", critical=True)
    if db.get_state(f"day_halt_{today}") and snap.halt == "ok":
        snap.halt = "day_halt"
    if snap.halt == "manual" and db.open_orders("entry"):
        _cancel_entries(acct)

    core.run_pm_exits(acct)
    core.manage_positions(acct, prices, near_close)
    tranche_at = dtime(*R.LEGACY["tranche_time_et"])
    if now.time() >= tranche_at:
        core.run_exit_plans(acct, prices)
    if near_close:
        core.bounce_entries(acct, snap, q, now)
    else:
        core.try_entries(acct, snap, q, now)
    core.publish_status(acct, snap)


def nightly(day, watch_date=None):
    tuning.load()
    acct = data.account()
    res = core.nightly(acct, progress=lambda m: print(m, flush=True), watch_date=watch_date)
    core.recap(acct, res)
    _mark("nightly", day)


def key_check(now, force=False):
    """Daily ping before the open, and every 30 min while the key is failing (so a rotated key is
    noticed quickly). Never raises: a monitoring hiccup must not stop the trading loop."""
    try:
        down = analyst.key_suspect()
        last = db.get_state("ai_key_pinged", 0) or 0
        daily = now.time() >= KEY_CHECK_ET and not _done("keycheck", now.date())
        if force or daily or (down and time.time() - last >= KEY_RECHECK_S):
            db.set_state("ai_key_pinged", time.time())
            _mark("keycheck", now.date())
            analyst.key_health()
    except Exception as e:
        print("key check failed:", notify.redact(e), flush=True)


def main():
    db.init()
    tuning.load()
    mode = "LIVE" if broker.live_enabled() else "DRY RUN (no orders sent)"
    notify.send(f"🏦 Claudio engine online: {mode}")
    key_check(data.now_et(), force=True)
    last_tick = None
    while True:
        key_check(data.now_et())
        try:
            from schwab_api.client import token_status
            ts = token_status()
            if ts["needs_login"]:
                notify.send("🔑 Schwab login expired: Claudio can't see or trade. Stops at Schwab are "
                            "still live. On the host machine: venv312/bin/python -m schwab_api.login --manual",
                            key="login", every=6 * 3600, critical=True)
                time.sleep(600)
                continue
            if ts["warn"]:
                notify.send(f"🔑 Schwab login expires in {ts['hours_left']:.0f}h. Renew on the host machine: "
                            "venv312/bin/python -m schwab_api.login --manual", key="warn", every=12 * 3600,
                            critical=True)

            now = data.now_et()
            today = now.date()
            s = _session(today)
            if s["open"]:
                o, c = datetime.fromisoformat(s["open"]), datetime.fromisoformat(s["close"])
                # catch-up: nightly for the previous session never ran (machine was off)
                last_sess = db.get_state("last_session_day")
                if now < o - timedelta(minutes=30) and last_sess and last_sess < str(today) and \
                        db.get_state("nightly_done") != last_sess and not _done("catchup", today):
                    _mark("catchup", today)
                    nightly(last_sess, watch_date=today)
                pm_at = o - timedelta(minutes=R.PREMARKET_MINUTES_BEFORE_OPEN)
                if pm_at <= now < o and not _done("premarket", today):
                    _mark("premarket", today)
                    core.premarket_check()
                if o - timedelta(minutes=15) <= now < c and not _done("morning", today):
                    morning(now)
                    _mark("morning", today)
                start = now.replace(hour=R.TRADE_START_ET[0], minute=R.TRADE_START_ET[1], second=0)
                end = min(now.replace(hour=R.TRADE_END_ET[0], minute=R.TRADE_END_ET[1], second=0),
                          c - timedelta(minutes=15))
                near = min(now.replace(hour=R.BOUNCE_CHECK_ET[0], minute=R.BOUNCE_CHECK_ET[1], second=0),
                           c - timedelta(minutes=25))
                if start <= now <= end:
                    is_near = now >= near and not _done("near_close", today)
                    if is_near or last_tick is None or (now - last_tick).seconds >= R.LOOP_SECONDS:
                        tick(now, near_close=is_near)
                        last_tick = now
                        if is_near:
                            _mark("near_close", today)
                if now >= c + timedelta(minutes=15) and not _done("nightly", today):
                    nightly(today)
            if now.weekday() == 5 and now.hour >= 10 and not _done("weekly", today):
                core.weekly_report()
                _mark("weekly", today)
        except Exception as e:
            core.on_error("loop", e)
            data.reset_client()
            time.sleep(60)
        time.sleep(30)


if __name__ == "__main__":
    main()
