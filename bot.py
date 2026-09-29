"""Claudio Telegram bot: trading commands, one-call analysis, and chat.

The bot never places orders. It reads the engine's state from SQLite and can
only pause (/halt) or resume trading.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from config_local import OWNER, TELEGRAM_CHAT_ID  # noqa: E402
from secrets_local import FINNHUB_API_KEY, TELEGRAM_TOKEN  # noqa: E402
from config.settings import MODEL_MAIN  # noqa: E402
from engine import db  # noqa: E402
from engine.notify import redact  # noqa: E402


ALLOWED_ID = int(TELEGRAM_CHAT_ID)
API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
HISTORY_PATH = os.path.join(BASE, "conversation_history.json")
MAX_HISTORY = 10            # was 25: every message resends history, this was the cost leak
MAX_MSG_CHARS = 2000
HISTORY_LOCK = threading.Lock()
offset = 0


def send(chat_id, text):
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
        requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": chunk}, timeout=15)


# ── history ──────────────────────────────────────────────────────────────
def load_history():
    with HISTORY_LOCK:
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except Exception:
            return {}


def save_history(h):
    with HISTORY_LOCK:
        with open(HISTORY_PATH, "w") as f:
            json.dump(h, f)


def add_to_history(chat_id, role, content):
    h = load_history()
    k = str(chat_id)
    h.setdefault(k, []).append({"role": role, "content": content[:MAX_MSG_CHARS],
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
    h[k] = h[k][-MAX_HISTORY:]
    save_history(h)


# ── trading views (read engine state; no broker calls) ───────────────────
def fmt_status():
    s = db.get_state("status")
    if not s:
        return "Engine hasn't published a status yet."
    day = s.get("day_start") or s["equity"]
    lines = [f"🏦 {'LIVE' if s.get('live') else 'DRY RUN'} | {s['ts']} ET",
             f"Equity ${s['equity']:,.0f} ({(s['equity'] / day - 1) * 100:+.2f}% today)",
             f"Settled cash ${s['settled_cash']:,.0f} | open risk {s['heat']:.1f}% | regime {s['regime']}",
             f"Trading: {'ON' if s['halt'] == 'ok' else s['halt'].upper()}"]
    return "\n".join(lines)


def fmt_positions():
    s = db.get_state("status")
    if not s or not s["positions"]:
        return "No positions."
    out = ["📋 POSITIONS"]
    for p in s["positions"]:
        r = f"{p['r']:+.2f}R" if p["r"] is not None else ""
        stop = f"stop ${p['stop']:.2f}" if p["stop"] else "NO STOP"
        out.append(f"{p['symbol']:6} {p['setup']:11} {p['pct']:4.1f}% ${p['price']:.2f} {stop} {r}")
    return "\n".join(out)


def fmt_watch():
    from engine.core import today_et
    rows = db.get_watch(str(today_et()))
    if not rows:
        return "Nothing on today's watchlist."
    out = [f"👀 WATCHLIST {today_et()}"]
    for w in rows:
        out.append(f"{w['symbol']} {w['setup']} above ${w['trigger']:.2f} stop ${w['stop']:.2f} "
                   f"[{w['status']}]{' half' if w['size_mult'] < 1 else ''}")
    return "\n".join(out)


def fmt_why(sym):
    pos = db.get_positions().get(sym, {})
    out = [f"🔎 {sym}"]
    if pos:
        out.append(f"{pos['setup']} since {pos['entry_date']} @ ${pos['entry_price']:.2f}, "
                   f"stop ${pos['stop']:.2f}. {pos.get('thesis') or ''}")
    for j in db.recent_journal(sym, 8):
        out.append(f"{j['ts'][5:16]} {j['action']}: {j['detail'][:180]}")
    return "\n".join(out) if len(out) > 1 else f"Nothing on record for {sym}."


def fmt_proposals():
    props = {k: v for k, v in (db.get_state("proposals", {}) or {}).items() if v["status"] == "open"}
    if not props:
        return "No open proposals."
    return "\n".join(f"#{k} {p['key']}: {p['current']} → {p['value']}\n   {p['why']}" for k, p in props.items()) + \
        "\n\n/approve N or /reject N"


def decide_proposal(pid, approve):
    from engine import tuning
    props = db.get_state("proposals", {}) or {}
    pid = pid.lstrip("#")
    p = props.get(pid)
    if not p or p["status"] != "open":
        return f"No open proposal #{pid}."
    if approve:
        try:
            old, new = tuning.apply(p["key"], p["value"], source=f"proposal #{pid}")
        except ValueError as e:
            return f"Can't apply #{pid}: {e}"
        p["status"] = "approved"
        msg = f"✅ Applied #{pid}: {p['key']} {old} → {new}. Takes effect at the next engine cycle."
    else:
        p["status"] = "rejected"
        msg = f"❌ Rejected #{pid}."
    props[pid] = p
    db.set_state("proposals", props)
    return msg


def analyze(sym):
    from engine import analyst, data, indicators
    data.update_bars([sym, "SPY"], pause=0.2)
    d = db.load_bars(sym, 300)
    if len(d) < 60:
        return f"Not enough price history for {sym}."
    d = indicators.enrich(d)
    t = d.iloc[-1]
    q = data.quotes([sym]).get(sym, {})
    data.refresh_fundamentals([sym])
    f = db.get_fundamentals(sym)
    num = lambda x: None if x != x else round(float(x), 2)   # NaN -> None
    payload = dict(symbol=sym, live_price=q.get("last"), last_close=num(t.close),
                   ma50=num(t.ma50), ma200=num(t.ma200), atr_pct=num(t.atr / t.close * 100),
                   rsi2=num(t.rsi2), ret_3m_pct=num(t.ret63 * 100),
                   off_52w_high_pct=num((1 - t.close / t.high252) * 100),
                   volume_vs_50d=num(d["volume"].iloc[-1] / t.vol50),
                   fundamentals={k: f.get(k) for k in ("marketCap", "peRatio", "pegRatio", "revChangeTTM",
                                                         "epsChangePercentTTM", "grossMarginTTM",
                                                         "totalDebtToEquity", "shortIntToFloat")},
                   next_earnings=str(data.next_earnings(sym) or "unknown"),
                   headlines=data.news(sym, limit=6), regime=db.get_state("regime"))
    return analyst.brief(payload)


# ── chat ─────────────────────────────────────────────────────────────────
CHAT_TOOLS = [
    {"name": "get_quote", "description": "Live quote (Schwab, real-time) for one or more tickers.",
     "input_schema": {"type": "object", "properties": {"tickers": {"type": "array", "items": {"type": "string"}}},
                      "required": ["tickers"]}},
    {"name": "get_news", "description": "Recent company headlines for a ticker (Finnhub).",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "get_portfolio", "description": "Claudio's current positions, stops, R-multiples, equity, "
                                              "open risk and regime (from the trading engine).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_trade_log", "description": "Why Claudio did something with a ticker: journal entries and thesis.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
]


def run_tool(name, args):
    try:
        if name == "get_quote":
            from engine import data
            return data.quotes([t.upper() for t in args["tickers"]][:10])
        if name == "get_news":
            from engine import data
            return data.news(args["ticker"].upper(), limit=6)
        if name == "get_portfolio":
            return db.get_state("status") or {}
        if name == "get_trade_log":
            return fmt_why(args["ticker"].upper())
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": redact(f"{name} failed: {e}")}


def run_claude(chat_id, text):
    prior = load_history().get(str(chat_id), [])[-MAX_HISTORY:]
    messages = [{"role": m["role"], "content": m["content"]} for m in prior]
    messages.append({"role": "user", "content": text})
    system = [{"type": "text", "cache_control": {"type": "ephemeral"}, "text": (
        f"You are Claudio, {OWNER}'s trading assistant and the voice of his automated swing-trading "
        f"system (Schwab cash account 'Luck'). Be direct and concise; this is Telegram. Use tools for "
        f"prices, news, the portfolio and the trade log: never quote a price from memory. You cannot "
        f"place trades; the engine does that by its rulebook. For a quick research brief tell {OWNER} "
        f"to send 'analyze TICKER'.")},
        {"type": "text", "text": f"Now: {datetime.now().strftime('%A %Y-%m-%d %H:%M')} (the host machine local time)."}]
    from engine.analyst import _client   # reads the key fresh: a rotated key works without a restart
    client, resp = _client(), None
    for _ in range(5):
        resp = client.messages.create(model=MODEL_MAIN, max_tokens=800, system=system,
                                      messages=messages, tools=CHAT_TOOLS)
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b.id,
             "content": json.dumps(run_tool(b.name, b.input), default=str)[:6000]}
            for b in resp.content if b.type == "tool_use"]})
    return "\n".join(b.text for b in resp.content if b.type == "text").strip() or "(no response)"


HELP = """🏦 CLAUDIO

TRADING
/status — equity, today, open risk, regime
/positions — holdings, stops, R
/watch — today's watchlist
/why TICKER — what Claudio did and why
/report — weekly review now (email)
/halt — stop new trades (stops stay live)
/resume — resume trading (resets drawdown baseline)
/keep T · /sell T — keep every share / sell all of an old holding (day-1 review)
/proposals — strategist's open rule changes
/approve N · /reject N — decide on one
/tuning — rule changes currently active

RESEARCH
analyze TICKER — one-call brief

CHAT
anything else — ask Claudio
clear — reset chat memory"""


def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    if user_id != ALLOWED_ID or not chat_id or not text:
        return
    t = text.lower()
    parts = text.split()
    arg = parts[1].upper() if len(parts) > 1 else None
    try:
        if t in ("/status", "status"):
            send(chat_id, fmt_status())
        elif t in ("/positions", "positions", "portfolio", "p"):
            send(chat_id, fmt_positions())
        elif t in ("/watch", "watch", "watchlist"):
            send(chat_id, fmt_watch())
        elif t.startswith(("/why", "why ")) and arg:
            send(chat_id, fmt_why(arg))
        elif t in ("/report", "report"):
            from engine.core import weekly_report
            weekly_report()
        elif t in ("/halt", "halt"):
            db.set_state("halt_manual", True)
            send(chat_id, "⏸ Halted: no new trades. Existing stops stay live at Schwab. /resume to continue.")
        elif t in ("/resume", "resume"):
            from engine import admin
            admin.resume()
        elif t.startswith(("/keep", "/sell")) and arg:
            ov = db.get_state("legacy_overrides", {}) or {}
            ov[arg] = "keep" if t.startswith("/keep") else "sell"
            db.set_state("legacy_overrides", ov)
            send(chat_id, f"Noted: {ov[arg]} {arg}" + (" (all shares, no trim)" if ov[arg] == "keep" else "")
                 + ". Applied at the day-1 review of your existing holdings.")
        elif t in ("/proposals", "proposals"):
            send(chat_id, fmt_proposals())
        elif t.startswith(("/approve", "/reject")) and len(parts) > 1:
            send(chat_id, decide_proposal(parts[1], approve=t.startswith("/approve")))
        elif t in ("/tuning", "tuning"):
            ov = db.get_state("overrides", {}) or {}
            send(chat_id, "Active tuning: " + (", ".join(f"{k}={v}" for k, v in ov.items()) or "none (defaults)"))
        elif t.startswith(("analyze ", "analyse ", "/analyze")) and arg:
            send(chat_id, f"🔬 {arg}…")
            send(chat_id, analyze(arg))
        elif t in ("clear", "reset", "/clear"):
            h = load_history()
            h[str(chat_id)] = []
            save_history(h)
            send(chat_id, "✅ Chat cleared.")
        elif t in ("help", "/help", "/start", "menu"):
            send(chat_id, HELP)
        else:
            reply = run_claude(chat_id, text)
            add_to_history(chat_id, "user", text)
            add_to_history(chat_id, "assistant", reply)
            send(chat_id, reply)
    except Exception as e:
        send(chat_id, f"❌ {redact(e)}")


if __name__ == "__main__":
    db.init()
    print("🏦 Claudio bot online", flush=True)
    errors = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 20},
                             timeout=25).json()
            errors = 0
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                if msg:
                    threading.Thread(target=handle_message, args=(msg,), daemon=True).start()
        except Exception as e:
            errors += 1
            print(f"poll error ({errors}): {redact(e)}", flush=True)
            if errors >= 15:
                sys.exit(1)       # launchd restarts with fresh sockets
            time.sleep(5)
