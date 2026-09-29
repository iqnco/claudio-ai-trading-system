"""Operator commands.

    venv312/bin/python -m engine.admin dryrun     # full cycle now, no orders sent
    venv312/bin/python -m engine.admin status
    venv312/bin/python -m engine.admin golive     # clear dry-run state, then flip the switch
    venv312/bin/python -m engine.admin halt | resume
    venv312/bin/python -m engine.admin aikey      # is the Claude API key working? (after a rotation)
    venv312/bin/python -m engine.admin metrics    # scorecard, benchmark, AI attribution
"""
import json
import re
import sys
from pathlib import Path

from . import broker, core, data, db, notify

ROOT = Path(__file__).resolve().parent.parent


def dryrun():
    if broker.live_enabled():
        sys.exit("Trading is LIVE. Dry run refuses to run in live mode.")
    db.init()
    acct = data.account()
    print(f"Account ...{acct['last4']} {acct['type']}: equity ${acct['equity']:,.2f}, "
          f"settled cash ${acct['settled_cash']:,.2f}")
    print("balances:", {k: acct["balances"].get(k) for k in (
        "cashBalance", "cashAvailableForTrading", "cashAvailableForWithdrawal", "unsettledCash")})
    if db.get_state("peak_equity") is None:
        db.set_state("peak_equity", acct["equity"])
    db.set_state("day_start_equity", acct["equity"])
    if not db.get_state("legacy_done"):
        print("\n== Day-1 review of existing holdings ==")
        for r in core.legacy_review(acct):
            print(f"{r['decision'].upper():5} {r['symbol']:6} stop {r.get('stop')} trim {r.get('trim', 0)} "
                  f"conv {r.get('conviction')} | {r.get('thesis')} | flags {r.get('red_flags')}")
    print("\n== Nightly scan + portfolio manager ==")
    from . import tuning
    tuning.load()
    res = core.nightly(acct, progress=lambda m: print("  ", m, flush=True))
    print(json.dumps({k: v for k, v in res.items() if k not in ("approved", "pm")}, indent=1, default=str))
    pm = res.get("pm") or {}
    print("\nPM MEMO:", pm.get("memo"))
    for h in pm.get("holdings", []):
        print(f"  HOLDING {h['symbol']:6} {h['action']:8} {h.get('new_stop') or ''} | {h['reason']}")
    for c, e in res["approved"]:
        print(f"  ENTRY {c['symbol']} {c['setup']} trigger {c['trigger']} stop {c['stop']} "
              f"x{e['size_mult']} | {e['thesis']}")
    for v in pm.get("vetoes", []):
        print(f"  VETO {v['symbol']}: {v['reason']}")
    snap = core.snapshot(acct)
    core.publish_status(acct, snap)
    print(f"\nOpen risk {snap.open_risk() / snap.equity:.2%} | halt {snap.halt} | regime {snap.regime}")
    db.set_state("done_nightly", str(core.today_et()))
    for w in db.get_watch(res["next_day"]):
        from . import risk
        u = db.get_universe().get(w["symbol"], {})
        qty, why = risk.size_trade(snap, w["symbol"], w["setup"], w["trigger"], w["stop"],
                                   u.get("sector", ""), u.get("market_cap"), w["size_mult"])
        print(f"  would size {w['symbol']}: {qty} sh — {why}")
    core.recap(acct, res)


def override(kind, symbol):
    ov = db.get_state("legacy_overrides", {}) or {}
    ov[symbol.upper()] = kind
    db.set_state("legacy_overrides", ov)
    print(f"{symbol.upper()}: owner says {kind} (applies at the day-1 review)")


def relegacy():
    """Dry run only: throw away the day-1 review and run it again (cached AI verdicts reused)."""
    if broker.live_enabled():
        sys.exit("Refusing in LIVE mode.")
    db.init()
    with db.conn() as c:
        c.execute("DELETE FROM positions WHERE legacy=1")
        c.execute("DELETE FROM orders WHERE order_id LIKE 'DRY%' AND kind IN ('stop','exit')")
    db.set_state("legacy_done", False)
    db.set_state("no_rebuy", {})
    acct = data.account()
    for r in core.legacy_review(acct):
        print(f"{r['decision'].upper():5} {r['symbol']:6} stop {r.get('stop')} trim {r.get('trim', 0)} "
              f"conv {r.get('conviction')} | {(r.get('thesis') or '')[:150]}")
    snap = core.snapshot(acct)
    print(f"open risk {snap.open_risk() / snap.equity:.2%}")


def decide(pid, approve):
    from . import tuning
    props = db.get_state("proposals", {}) or {}
    p = props.get(str(pid).lstrip("#"))
    if not p or p["status"] != "open":
        sys.exit(f"No open proposal #{pid}.")
    if approve:
        old, new = tuning.apply(p["key"], p["value"], source=f"proposal #{pid}")
        print(f"Applied #{pid}: {p['key']} {old} -> {new}")
    else:
        print(f"Rejected #{pid}")
    p["status"] = "approved" if approve else "rejected"
    props[str(pid).lstrip("#")] = p
    db.set_state("proposals", props)


def emailtest():
    from . import mailer
    ok = mailer.send("Claudio email test", "<p>If you can read this, reports will arrive here.</p>",
                     "If you can read this, reports will arrive here.")
    print("sent by email" if ok else "not configured / failed: sent to Telegram instead")


def metrics():
    from . import metrics as M
    n = M.score_pending()
    b, sc, at = M.benchmark(), M.scorecard(), M.attribution()
    print(f"scored {n} shadow candidates")
    if b:
        print(f"since {b['since']}: account {b['me_pct']:+.2f}% vs SPY {b['spy_pct']:+.2f}% "
              f"({b['diff_pct']:+.2f} points)")
    print("all time:", json.dumps(sc["all"]))
    print("last 20 :", json.dumps(sc["recent"]))
    for k, v in sc["by_setup"].items():
        print(f"  {k:9}", json.dumps(v))
    ex = M.execution_note()
    if ex:
        print(ex)
    blind = __import__("engine.db", fromlist=["db"]).get_state("earnings_unknown", []) or []
    if blind:
        print("no earnings date for:", ", ".join(blind))
    print("AI:", at["verdict"], f"({at['scored']} candidates scored)")
    for k, v in at["groups"].items():
        print(f"  {k:7}", json.dumps(v))


def aikey():
    from . import analyst
    ok = analyst.key_health()
    st = analyst.key_status()
    if ok:
        print("Claude API key works.")
    elif ok is None:
        print("Couldn't tell (network or Anthropic having trouble). Try again in a few minutes.")
    else:
        print(f"Claude API key NOT working: {analyst.KIND_TEXT.get(st.get('kind'), st.get('kind'))}\n"
              f"Anthropic said: {st.get('detail')}")


def status():
    print(json.dumps(db.get_state("status"), indent=1))
    for j in db.recent_journal(limit=15):
        print(j["ts"], j["symbol"], j["action"], j["detail"][:160])


def golive():
    cfg = ROOT / "config_local.py"
    text = cfg.read_text()
    if broker.live_enabled():
        sys.exit("Already live.")
    with db.conn() as c:
        n = c.execute("DELETE FROM orders WHERE order_id LIKE 'DRY%'").rowcount
        m = c.execute("DELETE FROM positions").rowcount          # all dry-run bookkeeping
        c.execute("DELETE FROM watchlist")
        c.execute("DELETE FROM state WHERE key IN ('peak_equity','eod_equity','day_start_equity',"
                  "'equity_curve','no_rebuy','legacy_verdicts','halt_full','halt_manual','pm_exits',"
                  "'status','legacy_done','bar_ctx','recorded_exits') OR key LIKE 'filled_entries_%' "
                  "OR key LIKE 'day_halt_%' OR key LIKE 'done_%'")
    db.set_state("legacy_done", False)
    new = re.sub(r"^SCHWAB_TRADING_ENABLED\s*=.*$", "SCHWAB_TRADING_ENABLED = True", text, flags=re.M)
    cfg.write_text(new)
    print(f"Cleared {n} dry orders and {m} dry positions. SCHWAB_TRADING_ENABLED = True.")
    print("Next US open: 9:15 ET review of holdings (stops first), sells from 10:30 ET.")
    print("Restart the engine: launchctl kickstart -k gui/$(id -u)/com.example.claudio-engine")


def halt():
    db.set_state("halt_manual", True)
    notify.send("⏸ Manual halt: no new trades. Stops stay live. Resume: engine.admin resume", critical=True)


def resume():
    acct = data.account()
    db.set_state("halt_manual", False)
    db.set_state("halt_full", False)
    db.set_state("peak_equity", acct["equity"])   # drawdown is measured from here on
    notify.send(f"▶️ Resumed. Drawdown baseline reset to ${acct['equity']:,.0f}.", critical=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in ("keep", "sell"):
        override(cmd, sys.argv[2])
    elif cmd in ("approve", "reject"):
        decide(sys.argv[2], cmd == "approve")
    else:
        {"dryrun": dryrun, "status": status, "golive": golive, "halt": halt, "resume": resume,
         "emailtest": emailtest, "relegacy": relegacy, "aikey": aikey,
         "metrics": metrics}[cmd]()
