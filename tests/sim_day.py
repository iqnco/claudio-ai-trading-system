"""Offline end-to-end simulation of a trading day. No network, no Schwab, no LLM, no orders.

    CLAUDIO_DB=/tmp/sim.db venv312/bin/python -m tests.sim_day

Fakes the market (synthetic bars with one momentum breakout, one oversold bounce, three
legacy holdings) and scripts the AI's answers, then runs: day-1 legacy review ->
nightly scan + PM -> pre-market -> morning -> intraday ticks (entry trigger, exit
tranches, PM exit) -> near-close -> nightly recap -> weekly review. Fails loudly on
any exception or broken invariant.
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

os.environ.setdefault("CLAUDIO_DB", os.path.join(tempfile.mkdtemp(), "sim.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from engine import analyst, broker, core, data, db, mailer, notify, rules as R, run, tuning  # noqa: E402

assert not broker.live_enabled(), "simulation must run in dry mode"
SENT, MAILS = [], []
notify.send = lambda text, key=None, every=0, critical=False: SENT.append(text)
mailer.send = lambda subj, body, text: MAILS.append((subj, mailer.page(subj, body), text)) or True

TODAY = data.now_et().date()
rng = np.random.default_rng(7)


def walk(start, end, n, noise=0.01):
    base = np.linspace(start, end, n)
    return base * (1 + rng.normal(0, noise, n))


SERIES = {
    "SPY": walk(500, 600, 300, 0.004),
    "MOMO": np.concatenate([walk(40, 98, 290, 0.006), [99.2, 99.5, 99.0, 99.6, 99.4, 99.8, 99.3, 99.7, 99.5, 99.6]]),
    "BNCE": np.concatenate([walk(60, 110, 294, 0.005), [110, 107, 104, 101, 99, 97.5]]),
    "LEG1": walk(30, 45, 300, 0.008),                    # healthy uptrend -> keep
    "LEG2": walk(80, 40, 300, 0.008),                    # downtrend -> exit
    "LEG3": walk(20, 22, 300, 0.01),
    "FILL": walk(50, 55, 300, 0.01),
}
UNI = [dict(symbol=s, name=f"{s} Inc", sector="Technology" if s in ("MOMO", "LEG1") else "Industrials",
            industry="Stuff", market_cap=5e9, ipo_year=2010) for s in SERIES if s != "SPY"]


def fake_bars(symbols, **kw):
    days = [TODAY - timedelta(days=i) for i in range(500)]
    days = [d for d in reversed(days) if d.weekday() < 5 and d < TODAY][-300:]
    for s in symbols:
        if s not in SERIES:
            continue
        c = SERIES[s][-len(days):]
        vol = np.full(len(c), 2e6)
        if s == "MOMO":
            vol[-1] = 2e6
        db.save_bars(s, [(str(d), float(x * 0.998), float(x * 1.01), float(x * 0.99), float(x), float(v))
                         for d, x, v in zip(days, c, vol)])


LIVE = {s: float(v[-1]) for s, v in SERIES.items()}
ACCOUNT = dict(hash="HASH", last4="0043", type="CASH", equity=None, settled_cash=6000.0, cash=6000.0,
               unsettled=0.0, balances={},
               positions={"LEG1": dict(qty=40, avg=35.0), "LEG2": dict(qty=30, avg=70.0),
                          "LEG3": dict(qty=50, avg=21.0)})


def fake_account():
    for s, p in ACCOUNT["positions"].items():
        p["price"] = LIVE[s]
        p["value"] = p["qty"] * LIVE[s]
    ACCOUNT["equity"] = ACCOUNT["settled_cash"] + sum(p["value"] for p in ACCOUNT["positions"].values())
    return json.loads(json.dumps(ACCOUNT))


def fake_quotes(symbols):
    out = {}
    for s in symbols:
        if s == "$VIX":
            out[s] = dict(last=17.0)
        elif s in LIVE:
            px = LIVE[s]
            out[s] = dict(last=px, bid=px - 0.02, ask=px + 0.02, volume=5e6, open=px, high=px, low=px,
                          prev_close=px)
    return out


data.account = fake_account
data.update_bars = fake_bars
data.refresh_universe = lambda: db.save_universe(UNI) or len(UNI)
data.refresh_fundamentals = lambda syms, **k: [db.save_fundamentals(s, dict(symbol=s, peRatio=20)) for s in syms]
data.quotes = fake_quotes
data.next_earnings = lambda s: None
data.earnings_dates = lambda s, **k: []
data.news = lambda s, **k: [dict(date=str(TODAY), headline=f"{s} does a thing", summary="", source="x")]
data.market_hours = lambda day=None: (datetime.combine(TODAY, datetime.min.time()).replace(hour=9, minute=30, tzinfo=data.ET),
                                      datetime.combine(TODAY, datetime.min.time()).replace(hour=16, tzinfo=data.ET))
CALLS = []


def fake_call(prompt, payload, max_tokens, with_strategy=True):
    CALLS.append(prompt[:40])
    assert "Claudio investment policy" in analyst.strategy_context()
    if prompt.startswith("You are reviewing a position"):
        keep = payload["symbol"] in ("LEG1", "LEG2")     # LEG2 keep -> engine downtrend override
        return json.dumps(dict(decision="keep" if keep else "exit", conviction=4 if keep else 2,
                               thesis=f"{payload['symbol']} scripted", red_flags=[]))
    if prompt.startswith("You are Claudio's portfolio manager. It is after"):
        cands = [c["symbol"] for c in payload["candidates"]]
        held = {h["symbol"]: h for h in payload["holdings"]}
        hs = []
        if "LEG1" in held:
            hs.append(dict(symbol="LEG1", action="tighten", new_stop=round(held["LEG1"]["stop"] + 0.5, 2), reason="protect"))
            hs.append(dict(symbol="LEG1", action="tighten", new_stop=1.0, reason="bad stop, must be dropped"))
        return json.dumps(dict(memo="Scripted memo.", holdings=hs,
                               entries=[dict(symbol=s, size="full", thesis="scripted", invalidation="x")
                                        for s in cands] + [dict(symbol="NOPE", size="full")],
                               vetoes=[], themes=["test"]))
    if prompt.startswith("You are Claudio's portfolio manager, 30"):
        return json.dumps(dict(exit_at_open=[], cancel_entries=[], note=""))
    if prompt.startswith("You are Claudio's strategist"):
        return json.dumps(dict(memo="Weekly scripted.", proposals=[
            dict(key="MOMENTUM.vol_mult", value=1.7, why="t"), dict(key="MAX_OPEN_RISK", value=0.2, why="illegal")]))
    raise AssertionError(f"unexpected prompt {prompt[:60]}")


analyst._call = fake_call


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


db.init()
tuning.load()
acct = fake_account()
print("equity", round(acct["equity"], 2))

print("1) day-1 legacy review")
db.set_state("peak_equity", acct["equity"])
db.set_state("day_start_equity", acct["equity"])
reviews = core.legacy_review(acct)
pos = db.get_positions()
check(set(pos) == {"LEG1", "LEG2", "LEG3"}, "all legacy holdings registered")
check(pos["LEG2"]["setup"] == "legacy_exit", "downtrending LEG2 overridden to exit despite AI keep")
check(pos["LEG3"]["setup"] == "legacy_exit", "LEG3 exit")
check(all(p["stop_order_id"] for p in pos.values()), "every legacy holding has a stop order")
check(all(p["stop"] < LIVE[s] for s, p in pos.items()), "stops below price")
check(any("day-1 review" in m[0] for m in MAILS), "day-1 review emailed")

print("2) nightly scan + PM")
res = core.nightly(fake_account(), progress=lambda m: None, watch_date=TODAY)
print("   shown:", res["shown"], "capacity:", res["capacity"], "regime:", res["regime"])
check(res["pm"] is not None, "PM ran")
check(all(e["symbol"] != "NOPE" for e in res["pm"]["entries"]), "PM cannot add a symbol the scanner didn't find")
check(sum(1 for h in res["pm"]["holdings"] if h["symbol"] == "LEG1") == 1, "invalid tighten dropped, valid kept")
watch = db.get_watch(str(TODAY))
print("   watch:", [(w["symbol"], w["setup"], w["trigger"], w["stop"]) for w in watch])
core.recap(fake_account(), res)
check(any(m[0].startswith("Claudio ") and "dry run" in m[0] for m in MAILS), "daily report emailed")

print("3) pre-market + morning")
core.premarket_check()
run.morning(data.now_et())

print("4) intraday ticks")
for w in watch:
    if w["setup"] != "bounce":
        LIVE[w["symbol"]] = w["trigger"] * 1.002        # breakout triggers
now = data.now_et().replace(hour=11, minute=0)
run.tick(now, near_close=False)
entries = db.open_orders("entry")
print("   entry orders:", [(o["symbol"], o["qty"], o["price"], o["stop"]) for o in entries])
if [w for w in watch if w["setup"] != "bounce"]:
    check(len(entries) >= 1, "breakout produced a dry buy order with attached stop")
    for o in entries:
        spec = o["meta"]["spec"]
        check(spec.get("orderStrategyType") == "TRIGGER" and spec["childOrderStrategies"][0]["orderType"] == "STOP",
              f"{o['symbol']} buy is one-triggers-other with a STOP child")
        risk_usd = (o["price"] - o["stop"]) * o["qty"]
        check(risk_usd <= acct["equity"] * R.RISK_PER_TRADE + 1, f"{o['symbol']} risk ${risk_usd:.0f} within budget")
tr = db.get_positions()
check(any(o["kind"] == "exit" for o in db.open_orders()), "legacy exit tranche sold (dry)")

print("5) near close + nightly again + weekly")
run.tick(now.replace(hour=15, minute=36), near_close=True)
res2 = core.nightly(fake_account(), progress=lambda m: None)
core.recap(fake_account(), res2)
db.record_trade("MOMO", "momentum", str(TODAY), str(TODAY), 100, 104, 10, 2, "exit")
core.weekly_report()
props = db.get_state("proposals")
check(len(props) == 1 and list(props.values())[0]["key"] == "MOMENTUM.vol_mult", "illegal proposal dropped")
check(any("weekly" in m[0] for m in MAILS), "weekly emailed")
st = db.get_state("status")
check(st and st["positions"], "status published for the bot")
print("\nAI calls:", len(CALLS), "| telegram msgs:", len(SENT), "| emails:", len(MAILS))
with open(os.path.join(os.path.dirname(os.environ["CLAUDIO_DB"]), "sim_report.html"), "w") as f:
    f.write(MAILS[1][1])
print("SIMULATION PASSED")
