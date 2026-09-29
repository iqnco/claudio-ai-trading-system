"""SQLite state for the engine: bar cache, positions, orders, journal, flags.

Schwab is the source of truth for what we own and which orders are live.
This database remembers *why* we own it (setup, stop plan, thesis) and what
happened (journal, closed trades), which Schwab can't.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("CLAUDIO_DB", os.path.join(ROOT, "data", "engine.db"))
# Bars, universe and fundamentals are the same facts for every profile, so a second
# profile reads ONE copy rather than keeping its own and refreshing it twice a night.
# Unset (the Luck engine): the market data lives in DB_PATH like everything else.
MARKET_DB_PATH = os.environ.get("CLAUDIO_MARKET_DB") or DB_PATH
# A profile that borrows another's market data must not write to it.
MARKET_READONLY = os.environ.get("CLAUDIO_MARKET_READONLY") == "1"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS universe (
  symbol TEXT PRIMARY KEY, name TEXT, sector TEXT, industry TEXT,
  market_cap REAL, ipo_year INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol TEXT PRIMARY KEY, data TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY, setup TEXT, entry_date TEXT, entry_price REAL,
  qty REAL, initial_stop REAL, stop REAL, risk_per_share REAL,
  partial_done INTEGER DEFAULT 0, high_water REAL, max_exit_date TEXT,
  stop_order_id TEXT, sector TEXT, market_cap REAL, thesis TEXT,
  exit_plan TEXT, legacy INTEGER DEFAULT 0, updated TEXT);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, symbol TEXT, kind TEXT, side TEXT, qty REAL,
  price REAL, stop REAL, status TEXT, created TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS watchlist (
  date TEXT, symbol TEXT, setup TEXT, trigger REAL, stop REAL, score REAL,
  size_mult REAL DEFAULT 1.0, analyst TEXT, status TEXT DEFAULT 'pending',
  meta TEXT, PRIMARY KEY (date, symbol));
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, setup TEXT,
  entry_date TEXT, exit_date TEXT, entry_price REAL, exit_price REAL,
  qty REAL, pnl REAL, r_multiple REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, action TEXT,
  detail TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
-- Every candidate the AI portfolio manager was shown, and what it decided. Scored later
-- against the bars, so we can measure whether the AI's picking beats the scanner's ranking.
CREATE TABLE IF NOT EXISTS shadow (
  date TEXT, symbol TEXT, setup TEXT, decision TEXT, rank INTEGER, score REAL,
  trigger REAL, stop REAL, size TEXT, reason TEXT, r REAL, days INTEGER,
  outcome TEXT, scored TEXT, PRIMARY KEY (date, symbol));
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


@contextmanager
def market():
    """Connection for bars / universe / fundamentals. Same file as conn() unless a
    profile points CLAUDIO_MARKET_DB at another engine's database."""
    if MARKET_DB_PATH == DB_PATH:
        with conn() as c:
            yield c
        return
    with _lock:
        c = sqlite3.connect(MARKET_DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            if not MARKET_READONLY:
                c.commit()
        finally:
            c.close()


def _no_writes(what):
    if MARKET_READONLY:
        raise RuntimeError(
            f"refusing to {what}: this profile reads market data from "
            f"{MARKET_DB_PATH} and must not write to it (CLAUDIO_MARKET_READONLY=1)")


def init():
    with conn() as c:
        c.executescript(SCHEMA)


# ── key/value state ──────────────────────────────────────────────────────
def get_state(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_state(key, value):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))


def all_state_prefix(prefix):
    """{key: value} for every state key starting with prefix. Used for per-month counts."""
    with conn() as c:
        rows = c.execute("SELECT key, value FROM state WHERE key LIKE ?", (prefix + "%",)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            pass
    return out


# ── journal ──────────────────────────────────────────────────────────────
def log(action, symbol=None, **detail):
    with conn() as c:
        c.execute("INSERT INTO journal (ts,symbol,action,detail) VALUES (?,?,?,?)",
                  (now_iso(), symbol, action, json.dumps(detail, default=str)))


def recent_journal(symbol=None, limit=20):
    with conn() as c:
        if symbol:
            rows = c.execute("SELECT * FROM journal WHERE symbol=? ORDER BY id DESC LIMIT ?",
                             (symbol, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── positions ────────────────────────────────────────────────────────────
POS_COLS = ("symbol setup entry_date entry_price qty initial_stop stop risk_per_share "
            "partial_done high_water max_exit_date stop_order_id sector market_cap "
            "thesis exit_plan legacy updated").split()


def get_positions():
    with conn() as c:
        return {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM positions")}


def upsert_position(p):
    p = {**p, "updated": now_iso()}
    cols = [k for k in POS_COLS if k in p]
    with conn() as c:
        c.execute(f"INSERT OR REPLACE INTO positions ({','.join(cols)}) VALUES "
                  f"({','.join('?' * len(cols))})", [p[k] for k in cols])


def update_position(symbol, **fields):
    fields["updated"] = now_iso()
    sets = ",".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE positions SET {sets} WHERE symbol=?", [*fields.values(), symbol])


def delete_position(symbol):
    with conn() as c:
        c.execute("DELETE FROM positions WHERE symbol=?", (symbol,))


# ── orders ───────────────────────────────────────────────────────────────
def record_order(order_id, symbol, kind, side, qty, price=None, stop=None,
                 status="WORKING", **meta):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (str(order_id), symbol, kind, side, qty, price, stop, status,
                   now_iso(), json.dumps(meta, default=str)))


def open_orders(kind=None):
    q = "SELECT * FROM orders WHERE status IN ('WORKING','QUEUED','ACCEPTED','PENDING_ACTIVATION','AWAITING_PARENT_ORDER','NEW','DRY')"
    args = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    with conn() as c:
        rows = c.execute(q, args).fetchall()
    return [{**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in rows]


def set_order_status(order_id, status):
    with conn() as c:
        c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, str(order_id)))


# ── trades ───────────────────────────────────────────────────────────────
def record_trade(symbol, setup, entry_date, exit_date, entry_price, exit_price,
                 qty, risk_per_share, reason):
    pnl = (exit_price - entry_price) * qty
    r = (exit_price - entry_price) / risk_per_share if risk_per_share else None
    with conn() as c:
        c.execute("INSERT INTO trades (symbol,setup,entry_date,exit_date,entry_price,"
                  "exit_price,qty,pnl,r_multiple,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (symbol, setup, entry_date, exit_date, entry_price, exit_price, qty,
                   pnl, r, reason))
    return pnl, r


def trades(since=None):
    with conn() as c:
        if since:
            rows = c.execute("SELECT * FROM trades WHERE exit_date>=? ORDER BY id", (since,))
        else:
            rows = c.execute("SELECT * FROM trades ORDER BY id")
        return [dict(r) for r in rows]


def last_stopout(symbol):
    with conn() as c:
        r = c.execute("SELECT exit_date FROM trades WHERE symbol=? AND reason LIKE 'stop%' "
                      "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    return r["exit_date"] if r else None


# ── shadow book (AI attribution) ─────────────────────────────────────────
def save_shadow(date, symbol, setup, decision, rank, score, trigger, stop,
                size=None, reason=None):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO shadow (date,symbol,setup,decision,rank,score,"
                  "trigger,stop,size,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (date, symbol, setup, decision, rank, score, trigger, stop, size,
                   (reason or "")[:300]))


def shadow(unscored_before=None, since=None):
    with conn() as c:
        if unscored_before:
            rows = c.execute("SELECT * FROM shadow WHERE scored IS NULL AND date<=? ORDER BY date",
                             (unscored_before,))
        elif since:
            rows = c.execute("SELECT * FROM shadow WHERE date>=? ORDER BY date", (since,))
        else:
            rows = c.execute("SELECT * FROM shadow ORDER BY date")
        return [dict(r) for r in rows]


def score_shadow(date, symbol, r, days, outcome):
    with conn() as c:
        c.execute("UPDATE shadow SET r=?, days=?, outcome=?, scored=? WHERE date=? AND symbol=?",
                  (r, days, (outcome or "")[:80], now_iso(), date, symbol))


# ── watchlist ────────────────────────────────────────────────────────────
def save_watch(date, symbol, setup, trigger, stop, score, size_mult=1.0,
               analyst=None, status="pending", **meta):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO watchlist VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (date, symbol, setup, trigger, stop, score, size_mult,
                   json.dumps(analyst) if analyst else None, status,
                   json.dumps(meta, default=str)))


def get_watch(date, status=None):
    with conn() as c:
        if status:
            rows = c.execute("SELECT * FROM watchlist WHERE date=? AND status=?", (date, status))
        else:
            rows = c.execute("SELECT * FROM watchlist WHERE date=?", (date,))
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"] or "{}")
            d["analyst"] = json.loads(d["analyst"]) if d["analyst"] else None
            out.append(d)
        return out


def set_watch_status(date, symbol, status):
    with conn() as c:
        c.execute("UPDATE watchlist SET status=? WHERE date=? AND symbol=?", (status, date, symbol))


# ── bars ─────────────────────────────────────────────────────────────────
def save_bars(symbol, candles):
    _no_writes("save bars")
    rows = [(symbol, d, o, h, l, cl, v) for d, o, h, l, cl, v in candles]
    with market() as c:
        c.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", rows)


def last_bar_date(symbol):
    with market() as c:
        r = c.execute("SELECT MAX(date) d FROM bars WHERE symbol=?", (symbol,)).fetchone()
    return r["d"] if r else None


def load_bars(symbol, limit=300):
    import pandas as pd
    with market() as c:
        rows = c.execute("SELECT date,open,high,low,close,volume FROM bars WHERE symbol=? "
                         "ORDER BY date DESC LIMIT ?", (symbol, limit)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows[::-1]])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


# ── universe / fundamentals ──────────────────────────────────────────────
def save_universe(rows):
    _no_writes("replace the universe")
    with market() as c:
        c.execute("DELETE FROM universe")
        c.executemany("INSERT INTO universe VALUES (?,?,?,?,?,?,?)",
                      [(r["symbol"], r["name"], r["sector"], r["industry"], r["market_cap"],
                        r["ipo_year"], now_iso()) for r in rows])


def get_universe():
    with market() as c:
        return {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM universe")}


def save_fundamentals(symbol, data):
    _no_writes("save fundamentals")
    with market() as c:
        c.execute("INSERT OR REPLACE INTO fundamentals VALUES (?,?,?)",
                  (symbol, json.dumps(data), now_iso()))


def get_fundamentals(symbol=None):
    with market() as c:
        if symbol:
            r = c.execute("SELECT data FROM fundamentals WHERE symbol=?", (symbol,)).fetchone()
            return json.loads(r["data"]) if r else {}
        return {r["symbol"]: json.loads(r["data"]) for r in
                c.execute("SELECT symbol,data FROM fundamentals")}
