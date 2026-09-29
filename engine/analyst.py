"""The AI side of Claudio: portfolio manager, pre-market check, weekly strategist.

All three read STRATEGY.md plus the live parameter values, and their own recent
memos, so the strategy stays one coherent thing from day to day. Their output is
validated in code before anything happens:
  * entries: only symbols the scanner found; size can only be full or half
  * holdings: hold, tighten (stop only up, stays below price) or exit
  * tuning: proposals only, applied by the owner, inside tuning.TUNABLE bounds
"""
import importlib
import json
import re
import sys
import time
from pathlib import Path

from . import db, notify
from . import rules as R

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DAILY_CALL_CAP = 40      # hard ceiling on engine LLM calls per day
PRICE_IN, PRICE_OUT = 3.0, 15.0   # $/M tokens, for the cost line in reports


# ── plumbing ─────────────────────────────────────────────────────────────
def _api_key():
    """Read the key fresh every call, so a rotated key in secrets_local.py works without a restart."""
    import secrets_local
    importlib.reload(secrets_local)
    return getattr(secrets_local, "ANTHROPIC_API_KEY", "") or ""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=_api_key(), max_retries=2)


def _model():
    try:
        from config.settings import MODEL_MAIN
        return MODEL_MAIN
    except Exception:
        return "claude-sonnet-4-6"


def strategy_context():
    """The policy document + current parameter values: the constitution for every call.

    Each profile answers to its own policy. CashMoney is a different account with different
    rules, and handing its portfolio manager Luck's document would be worse than handing it none.
    """
    from . import tuning
    doc = (ROOT / ("STRATEGY_CASH.md" if R.PROFILE == "cash" else "STRATEGY.md")).read_text()
    params = {k: v["value"] for k, v in tuning.current().items()}
    fixed = dict(MAX_POSITIONS=R.MAX_POSITIONS, MAX_POSITION_PCT=R.MAX_POSITION_PCT,
                 MAX_POSITION_PCT_SMALLCAP=R.MAX_POSITION_PCT_SMALLCAP,
                 MAX_SECTOR_PCT=R.MAX_SECTOR_PCT, MAX_OPEN_RISK=R.MAX_OPEN_RISK,
                 DAILY_LOSS_HALT=R.DAILY_LOSS_HALT, TOTAL_DRAWDOWN_HALT=R.TOTAL_DRAWDOWN_HALT,
                 MOMENTUM=R.MOMENTUM, BOUNCE=R.BOUNCE, CATALYST=R.CATALYST, VALUE=R.VALUE)
    return (f"{doc}\n\n## Current parameter values\nTunable: {json.dumps(params)}\n"
            f"Fixed: {json.dumps(fixed)}")


def _budget_ok():
    from .data import now_et
    key = f"llm_calls_{now_et().date().isoformat()}"
    n = db.get_state(key, 0)
    if n >= DAILY_CALL_CAP:
        return False
    db.set_state(key, n + 1)
    return True


def _parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in reply: {text[:200]}")
    return json.loads(m.group(0))


def _call(role_prompt, payload, max_tokens, with_strategy=True):
    if not _budget_ok():
        raise RuntimeError("daily LLM call cap reached")
    system = []
    if with_strategy:
        system.append({"type": "text", "text": strategy_context(),
                       "cache_control": {"type": "ephemeral"}})
    system.append({"type": "text", "text": role_prompt})
    try:
        resp = _client().messages.create(
            model=_model(), max_tokens=max_tokens, temperature=0.2, system=system,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}])
    except Exception as e:
        kind = classify_error(e)
        if kind:
            mark_key(False, kind, _err_text(e))
        else:
            note_failure(_err_text(e))
        raise
    mark_key(True)
    u = getattr(resp, "usage", None)
    if u:
        tot = db.get_state("llm_tokens_total", {"in": 0, "out": 0})
        tot["in"] += u.input_tokens + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        tot["out"] += u.output_tokens
        db.set_state("llm_tokens_total", tot)
    return "".join(b.text for b in resp.content if b.type == "text")


# ── API key health ───────────────────────────────────────────────────────
# The key's owner can't see the Console, so the engine watches the key itself: any call that is
# refused for auth or billing reasons (and a tiny daily ping) flips state "ai_key" and emails the
# owner. Network blips, rate limits and overloads are not the key's fault and are ignored.
KEY_ALERT_EVERY = 24 * 3600
UNKNOWN_DOWN_AFTER = 6 * 3600   # unexplained failures: call it down after 6h (3+ tries) with no success
_CREDIT_WORDS = ("credit balance", "billing", "purchase credits", "usage limit", "spend limit",
                 "spending limit", "insufficient funds", "quota")
_AUTH_WORDS = ("disabled", "suspended", "x-api-key", "api key", "api_key")
KEY_HELP = ("To fix it, put a new key in ~/claudio-inc/secrets_local.py on the host machine "
            "(the line ANTHROPIC_API_KEY = \"sk-ant-...\"). The engine picks it up by itself within "
            "30 minutes, no restart needed, and emails you once it works. To check right away: "
            "cd ~/claudio-inc && venv312/bin/python -m engine.admin aikey")
KIND_TEXT = {"credit": "out of credit / billing limit reached",
             "auth": "key rejected (revoked, disabled or missing)",
             "model": "model not available to this key",
             "unknown": "every AI call has failed for 6+ hours"}


def _err_text(e):
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        m = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
        if m:
            return str(m)[:300]
    return str(getattr(e, "message", "") or e)[:300]


def classify_error(e):
    """'credit' | 'auth' | 'model' when the error means the key itself is unusable, else None."""
    status = getattr(e, "status_code", None)
    msg = _err_text(e).lower()
    if status is None:                     # failed before reaching Anthropic, e.g. no key configured
        return "auth" if ("api_key" in msg or "api key" in msg) else None
    if status in (400, 402, 403, 429) and any(w in msg for w in _CREDIT_WORDS):
        return "credit"
    if status in (401, 403) or (status == 400 and any(w in msg for w in _AUTH_WORDS)):
        return "auth"
    if status == 404 and "model" in msg:
        return "model"
    return None


def key_status():
    return db.get_state("ai_key") or {"ok": True}


def mark_key(ok, kind=None, detail=""):
    """Record the key's health; email on the way down (repeat daily) and on recovery."""
    st = key_status()
    now = time.time()
    if ok:
        if db.get_state("ai_key_fail"):
            db.set_state("ai_key_fail", None)
        if st.get("ok") is False:
            db.set_state("ai_key", dict(ok=True, checked=now))
            db.log("ai key ok", None, was=st.get("kind"))
            notify.send("✅ Claude API key is working again. AI reviews and new trades resume on the next "
                        "run (tonight's review after the close).", critical=True)
        elif not st.get("checked") or now - st["checked"] > 3600:
            db.set_state("ai_key", dict(ok=True, checked=now))
        return
    first = st.get("ok") is not False
    new = dict(ok=False, kind=kind, detail=str(detail)[:300], checked=now,
               since=now if first else st.get("since", now), alerted=st.get("alerted", 0))
    if first or now - (new["alerted"] or 0) >= KEY_ALERT_EVERY:
        new["alerted"] = now
        db.set_state("ai_key", new)
        if first:
            db.log("ai key down", None, kind=kind, detail=str(detail)[:300])
        from datetime import datetime
        from .data import ET
        since = datetime.fromtimestamp(new["since"], ET).strftime("%a %b %d %H:%M ET")
        notify.send(
            f"🔑 Claude API key stopped working: {KIND_TEXT.get(kind, kind)}\n\n"
            f"Anthropic said: {detail}\n(first failed {since})\n\n"
            "What keeps running: every stop order at Schwab, trailing stops, time and earnings exits, "
            "the kill switches and the planned sells. What pauses: new trades (Claudio takes none "
            "without the nightly AI review), the pre-market news check and the weekly review.\n\n"
            + KEY_HELP + "\n\nYou'll get this reminder once a day until it's fixed.", critical=True)
    else:
        db.set_state("ai_key", new)


def note_failure(detail):
    """A failure that doesn't say why (network, 5xx, odd 4xx). Harmless alone; if nothing has worked
    for UNKNOWN_DOWN_AFTER across 3+ tries, the key is treated as down so the owner hears about it."""
    now = time.time()
    f = db.get_state("ai_key_fail") or {}
    f = dict(first=f.get("first", now), n=f.get("n", 0) + 1, last=str(detail)[:300])
    db.set_state("ai_key_fail", f)
    if f["n"] >= 3 and now - f["first"] >= UNKNOWN_DOWN_AFTER:
        mark_key(False, "unknown", detail)


def key_suspect():
    """True while the key is down or failing unexplained: the engine then re-checks every 30 min."""
    return key_status().get("ok") is False or bool(db.get_state("ai_key_fail"))


def key_health():
    """1-token ping outside the daily call cap. True = works, False = key unusable, None = unknown
    (network trouble or an overloaded API: not the key's fault, state unchanged)."""
    try:
        _client().messages.create(model=_model(), max_tokens=1,
                                  messages=[{"role": "user", "content": "ping"}])
    except Exception as e:
        kind = classify_error(e)
        if kind:
            mark_key(False, kind, _err_text(e))
            return False
        db.log("ai key ping failed", None, error=_err_text(e))
        note_failure(_err_text(e))
        return None
    mark_key(True)
    return True


def cost_to_date():
    t = db.get_state("llm_tokens_total", {"in": 0, "out": 0})
    return t["in"] / 1e6 * PRICE_IN + t["out"] / 1e6 * PRICE_OUT


# ── nightly portfolio manager ────────────────────────────────────────────
PM_PROMPT = """You are Claudio's portfolio manager. It is after the US close. The rules engine has \
scanned the market and computed stops and limits. You make the judgment calls for tomorrow, following \
the investment policy above. You manage the whole book as one portfolio.

You receive: account state, market regime, every holding (setup, entry, stop, R now, days held, \
thesis, latest headlines, next earnings), tonight's scanner candidates (with stops the engine computed, \
fundamentals and headlines), recent closed trades and your own previous memos.

Decide:
1. Holdings: for each one, "hold", "tighten" (give new_stop: higher than the current stop and below \
the price) or "exit" (sold at tomorrow's open). Exit when the thesis is broken or the position no \
longer fits its setup. Don't churn winners that are working. Positions marked legacy_exit are \
already being sold: leave them out. Positions marked owner_kept are the owner's decision: always \
"hold" them, but say in the memo if you think one is a mistake.
2. Entries: the candidates worth taking, best first. Capacity says roughly how many new trades the \
risk budget allows; you may rank up to capacity + 2 (backups fill only if earlier ones don't trigger). \
Use "half" when the idea is good but something specific worries you. Take none if nothing is good.
3. Vetoes: any candidate you reject for a specific reason (skip ones that are simply weaker).
4. Memo: 3-6 plain sentences for the owner: what you did, why, the main risk you see, and what \
changed since your last memo.

Reply with ONLY JSON:
{"memo": "...",
 "holdings": [{"symbol": "X", "action": "hold|tighten|exit", "new_stop": null, "reason": "..."}],
 "entries": [{"symbol": "X", "size": "full|half", "thesis": "<=2 sentences",
              "invalidation": "what proves it wrong"}],
 "vetoes": [{"symbol": "X", "reason": "..."}],
 "themes": ["short phrases worth watching"]}"""


def validate_pm(v, candidates, holdings, prices, capacity):
    """Keep only decisions the engine is allowed to act on. Pure; returns (clean, dropped)."""
    cand = {c["symbol"]: c for c in candidates}
    held = {h["symbol"]: h for h in holdings}
    clean = dict(memo=str(v.get("memo", ""))[:1500], holdings=[], entries=[], vetoes=[],
                 themes=[str(t)[:80] for t in (v.get("themes") or [])][:5])
    dropped = []
    for h in v.get("holdings") or []:
        sym = str(h.get("symbol", "")).upper()
        act = h.get("action")
        if sym not in held or held[sym].get("setup") == "legacy_exit" or act not in ("hold", "tighten", "exit"):
            dropped.append(("holding", sym, "unknown symbol or action"))
            continue
        if held[sym].get("owner_kept") and act != "hold":
            dropped.append(("holding", sym, "owner-kept: only its stop can sell it"))
            continue
        if act == "tighten":
            try:
                ns = round(float(h.get("new_stop")), 2)
            except (TypeError, ValueError):
                dropped.append(("tighten", sym, "no stop given"))
                continue
            px = prices.get(sym) or held[sym].get("price")
            if ns <= (held[sym].get("stop") or 0) or not px or ns > px * (1 - R.PM_MIN_TIGHTEN_GAP):
                dropped.append(("tighten", sym, f"stop {ns} not above current and below price"))
                continue
            h = dict(h, new_stop=ns)
        clean["holdings"].append(dict(symbol=sym, action=act, new_stop=h.get("new_stop"),
                                      reason=str(h.get("reason", ""))[:300]))
    seen = set()
    for e in v.get("entries") or []:
        sym = str(e.get("symbol", "")).upper()
        if sym not in cand or sym in seen or sym in held:
            dropped.append(("entry", sym, "not a scanner candidate"))
            continue
        seen.add(sym)
        if len(clean["entries"]) >= (capacity + 2 if capacity > 0 else 0):
            dropped.append(("entry", sym, "beyond capacity"))
            continue
        clean["entries"].append(dict(symbol=sym, size_mult=0.5 if e.get("size") == "half" else 1.0,
                                     thesis=str(e.get("thesis", ""))[:400],
                                     invalidation=str(e.get("invalidation", ""))[:300]))
    for x in v.get("vetoes") or []:
        sym = str(x.get("symbol", "")).upper()
        if sym in cand and sym not in seen:
            clean["vetoes"].append(dict(symbol=sym, reason=str(x.get("reason", ""))[:300]))
    return clean, dropped


def pm_review(payload):
    """Returns validated decisions, or None if the call failed (engine then takes no new trades)."""
    try:
        raw = _parse(_call(PM_PROMPT, payload, R.PM_MAX_TOKENS))
    except Exception as e:
        db.log("pm_error", None, error=str(e)[:300])
        return None
    clean, dropped = validate_pm(raw, payload["candidates"], payload["holdings"],
                                 {h["symbol"]: h["price"] for h in payload["holdings"]},
                                 payload["account"]["capacity"])
    if dropped:
        db.log("pm_dropped", None, dropped=dropped)
    return clean


# ── CashMoney portfolio manager ──────────────────────────────────────────
# The screen is mechanical and a mechanical value screen has a known failure mode: it finds
# commodity producers at the top of their earnings cycle, where the low multiple is a symptom
# rather than an opportunity. The rulebook now guards against the versions of that we can
# measure. This call is for the versions we cannot.
CASH_PM_PROMPT = """You are the portfolio manager for CashMoney, the careful account, \
following the investment policy above. It is after the US close.

The rules engine has already done the mechanical work: it screened the market, applied every \
hard limit, and computed the entry and stop for each candidate. Everything you see is legal \
under the policy. Your job is the judgment the rules cannot encode, and your default answer is \
no. A week with no trades is a good week in this account.

Be hardest on this: **a cheap multiple on a cyclical business at the top of its cycle is not \
value.** A miner after the metal has run, a shipper after freight rates spiked, a lender after \
a rate cycle — trailing earnings are at a high, so the P/E looks small, and then earnings \
normalise and the discount was arithmetic rather than opportunity. The screen already rejects \
the measurable versions (earnings that more than doubled, margins visibly rolling over, \
multiples priced for collapse). You are looking for the ones that got through. Ask of every \
candidate: if this company's last twelve months were its best in a decade, what does it earn in \
a normal year, and is it still cheap on that number?

Also weigh:
- **Concentration.** Two names with the same underlying driver are one position with twice the \
size, whatever their sectors say. Gold miners are one bet. Tankers are one bet.
- **Durability.** This account holds for years. Is the business still here and still earning in \
five? A cheap multiple on a melting ice cube is not a bargain.
- **Whether the discount has a reason.** Say what you think the market is worried about, and \
whether you think it is wrong. "I cannot tell" is a veto, not a shrug.

Decide:
1. Entries: candidates worth taking, best first, at most `capacity`. Use "half" when the idea is \
sound but something specific worries you. Take none if nothing is good — say so plainly.
2. Vetoes: any candidate you reject, with the specific reason. Name the cyclicality where you see it.
3. Holdings: for each, "hold", "tighten" (new_stop above the current stop and below the price) or \
"exit" (thesis broken). Do not churn positions that are working.
4. Memo: 3-6 plain sentences for the owner: what you did, why, the main risk you see, and what \
changed since your last memo.

Reply with ONLY JSON:
{"memo": "...",
 "holdings": [{"symbol": "X", "action": "hold|tighten|exit", "new_stop": null, "reason": "..."}],
 "entries": [{"symbol": "X", "size": "full|half", "thesis": "<=2 sentences",
              "invalidation": "what proves it wrong"}],
 "vetoes": [{"symbol": "X", "reason": "..."}],
 "themes": ["short phrases worth watching"]}"""


def cash_review(payload):
    """The last step before anything is bought. None means the call failed, and a failed
    call must mean no new positions -- never a silent fall-through to the raw screen."""
    try:
        raw = _parse(_call(CASH_PM_PROMPT, payload, R.PM_MAX_TOKENS))
    except Exception as e:
        db.log("cash_pm_error", None, error=str(e)[:300])
        return None
    clean, dropped = validate_pm(raw, payload["candidates"], payload["holdings"],
                                 {h["symbol"]: h.get("price") for h in payload["holdings"]},
                                 payload["account"]["capacity"])
    if dropped:
        db.log("cash_pm_dropped", None, dropped=dropped)
    return clean


# ── pre-market check ─────────────────────────────────────────────────────
PREMARKET_PROMPT = """You are Claudio's portfolio manager, 30 minutes before the US open. Overnight \
headlines arrived for some holdings or planned entries. Last night's plan and memo are included.

Only act on news that clearly changes the picture: thesis-breaking news on a holding means exit at \
the open; bad news on a planned entry means cancel it. Routine news, price-target chatter and \
recaps are not reasons to act. Doing nothing is the usual answer.

Reply with ONLY JSON:
{"exit_at_open": [{"symbol": "X", "reason": "..."}],
 "cancel_entries": [{"symbol": "X", "reason": "..."}],
 "note": "one or two sentences, or empty"}"""


def premarket(payload):
    try:
        v = _parse(_call(PREMARKET_PROMPT, payload, 800))
    except Exception as e:
        db.log("premarket_error", None, error=str(e)[:300])
        return None
    held = set(payload["holdings"])
    watch = set(payload["watchlist"])
    return dict(
        exit_at_open=[dict(symbol=x["symbol"].upper(), reason=str(x.get("reason", ""))[:300])
                      for x in v.get("exit_at_open") or [] if str(x.get("symbol", "")).upper() in held],
        cancel_entries=[dict(symbol=x["symbol"].upper(), reason=str(x.get("reason", ""))[:300])
                        for x in v.get("cancel_entries") or [] if str(x.get("symbol", "")).upper() in watch],
        note=str(v.get("note", ""))[:400])


# ── weekly strategist ────────────────────────────────────────────────────
WEEKLY_PROMPT = """You are Claudio's strategist doing the weekly review. You get every closed trade, \
stats by setup (all-time and last 4 weeks), missed and expired entries, the portfolio manager's memos \
this week, the equity curve, the market regime history and every tunable parameter with its allowed \
range.

Write an honest memo (6-10 sentences): what worked, what didn't, whether losses were the strategy \
working as designed (small, controlled) or a sign something is off, and what to watch next week.
Then propose at most 3 parameter changes, only where the evidence supports it (see the weekly-review \
section of the policy on thin data). Each proposal changes one tunable key to a value inside its range. \
Proposing nothing is fine and common.

Reply with ONLY JSON:
{"memo": "...", "proposals": [{"key": "MOMENTUM.vol_mult", "value": 1.8, "why": "..."}]}"""


def weekly(payload):
    from . import tuning
    try:
        v = _parse(_call(WEEKLY_PROMPT, payload, 1800))
    except Exception as e:
        db.log("weekly_error", None, error=str(e)[:300])
        return None
    props = []
    for p in (v.get("proposals") or [])[:3]:
        try:
            val = tuning.validate(p.get("key"), p.get("value"))
        except (ValueError, TypeError) as e:
            db.log("proposal_invalid", None, proposal=p, error=str(e))
            continue
        if val == tuning.get(p["key"]):
            continue
        props.append(dict(key=p["key"], value=val, current=tuning.get(p["key"]),
                          why=str(p.get("why", ""))[:400]))
    return dict(memo=str(v.get("memo", ""))[:3000], proposals=props)


# ── legacy holdings (day 1) ──────────────────────────────────────────────
LEGACY_PROMPT = """You are reviewing a position the owner bought before Claudio took over the account. \
Judge it as if it were a fresh buy today under the investment policy: the purchase price is irrelevant. \
Keep when the trend is intact (above its 50- or 200-day average, or recovering with a clear reason) and \
nothing in the news breaks the story. Exit when it's in a downtrend without an identifiable catalyst, \
when news breaks the thesis, or when the stop it needs makes the risk poor.

Reply with ONLY JSON:
{"decision": "keep" | "exit", "conviction": 1-5, "thesis": "<=2 sentences", "red_flags": ["..."]}"""


def review_legacy(symbol, metrics, fundamentals, headlines, next_earnings):
    payload = dict(symbol=symbol, metrics=metrics,
                   fundamentals={k: fundamentals.get(k) for k in (
                       "marketCap", "peRatio", "revChangeTTM", "epsChangePercentTTM",
                       "totalDebtToEquity") if fundamentals.get(k) is not None},
                   next_earnings=str(next_earnings) if next_earnings else "unknown",
                   headlines_last_7d=headlines or "none found")
    try:
        v = _parse(_call(LEGACY_PROMPT, payload, R.ANALYST_MAX_TOKENS))
    except Exception as e:
        db.log("analyst_error", symbol, error=str(e)[:300])
        return dict(decision="keep", conviction=0, thesis="analyst unavailable; kept with a stop",
                    red_flags=[str(e)[:120]])
    return dict(decision="exit" if v.get("decision") == "exit" else "keep",
                conviction=int(v.get("conviction") or 0), thesis=v.get("thesis", ""),
                red_flags=v.get("red_flags", []))


# ── on-demand brief (Telegram "analyze TICKER") ──────────────────────────
BRIEF_PROMPT = """Write a tight Telegram brief (max ~180 words) on one stock from the live numbers \
and headlines given: what the company is, trend and momentum in plain words, bull and bear case in \
one line each, whether any of the four setups applies right now and why, and the key level to watch. \
No prices you were not given. No hype."""


def brief(payload):
    return _call(BRIEF_PROMPT, payload, 500)
