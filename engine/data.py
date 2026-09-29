"""Market + account data. Schwab first; Finnhub for earnings/news; Nasdaq for the universe."""
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import db
from . import rules as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
PLAIN_ETFS = {  # liquid, unlevered; eligible for momentum / bounce only
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow 30",
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLI": "Industrials", "XLY": "Cons. Discretionary", "XLP": "Cons. Staples",
    "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Communication",
    "SMH": "Semiconductors", "IBB": "Biotech", "GLD": "Gold", "SLV": "Silver",
    "TLT": "20y Treasuries", "EEM": "Emerging Mkts", "EFA": "Developed ex-US",
    "KRE": "Regional Banks", "XBI": "Biotech (eq wt)", "ITA": "Aerospace & Defense",
}
_client = None


def client():
    global _client
    if _client is None:
        from schwab_api.client import get_client
        _client = get_client()
    return _client


def reset_client():
    global _client
    _client = None


def now_et():
    return datetime.now(ET)


def _get(fn, *a, retries=3, **kw):
    """Schwab call with backoff on 429/5xx. Returns parsed JSON."""
    for i in range(retries):
        r = fn(*a, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 + 3 * i)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


# ── calendar ─────────────────────────────────────────────────────────────
def market_hours(day=None):
    """(open_dt, close_dt) in ET for the regular session, or None if closed."""
    c = client()
    day = day or now_et().date()
    j = _get(c.get_market_hours, [c.MarketHours.Market.EQUITY],
             date=day)
    eq = j.get("equity", {})
    info = eq.get("EQ") or eq.get("equity") or next(iter(eq.values()), {})
    if not info.get("isOpen"):
        return None
    reg = info["sessionHours"]["regularMarket"][0]
    return (datetime.fromisoformat(reg["start"]).astimezone(ET),
            datetime.fromisoformat(reg["end"]).astimezone(ET))


# ── bars ─────────────────────────────────────────────────────────────────
def update_bars(symbols, full_days=420, pause=0.52, progress=None):
    """Incremental daily-bar refresh into SQLite. ~110 calls/min (Schwab allows 120)."""
    c = client()
    today = now_et().date()
    done = 0
    for sym in symbols:
        last = db.last_bar_date(sym)
        start = (datetime.fromisoformat(last) - timedelta(days=7)) if last else \
            datetime.now() - timedelta(days=full_days)
        if last and last >= str(today):
            continue
        try:
            j = _get(c.get_price_history_every_day, sym, start_datetime=start)
            candles = [(datetime.fromtimestamp(k["datetime"] / 1000, ET).date().isoformat(),
                        k["open"], k["high"], k["low"], k["close"], k["volume"])
                       for k in j.get("candles", [])]
            # a bar for today only counts once the session is over
            if now_et().hour < 16:
                candles = [k for k in candles if k[0] < str(today)]
            if candles:
                db.save_bars(sym, candles)
        except Exception as e:
            db.log("bars_error", sym, error=str(e)[:200])
        done += 1
        if progress and done % 250 == 0:
            progress(done)
        time.sleep(pause)


# ── quotes / fundamentals ────────────────────────────────────────────────
def quotes(symbols):
    """{symbol: {last, bid, ask, volume, open, high, low, prev_close}}"""
    c = client()
    out = {}
    syms = list(dict.fromkeys(symbols))
    for i in range(0, len(syms), 200):
        j = _get(c.get_quotes, syms[i:i + 200])
        for s, v in j.items():
            q = v.get("quote", {})
            out[s] = dict(last=q.get("lastPrice") or q.get("mark"), bid=q.get("bidPrice"),
                          ask=q.get("askPrice"), volume=q.get("totalVolume"),
                          open=q.get("openPrice"), high=q.get("highPrice"),
                          low=q.get("lowPrice"), prev_close=q.get("closePrice"))
    return out


def refresh_fundamentals(symbols, pause=0.6):
    c = client()
    syms = list(symbols)
    for i in range(0, len(syms), 50):
        try:
            j = _get(c.get_instruments, syms[i:i + 50], c.Instrument.Projection.FUNDAMENTAL)
            for ins in j.get("instruments", []):
                f = ins.get("fundamental") or {}
                if f.get("symbol"):
                    db.save_fundamentals(f["symbol"], f)
        except Exception as e:
            db.log("fundamentals_error", None, error=str(e)[:200])
        time.sleep(pause)


# ── account ──────────────────────────────────────────────────────────────
def account():
    """Balances + positions for the allowed (Luck) account only."""
    from schwab_api.client import account_hashes
    c = client()
    hashes = account_hashes(c)
    if len(hashes) != 1:
        raise RuntimeError(f"expected exactly one allowed account, got {len(hashes)}")
    last4, h = next(iter(hashes.items()))
    a = _get(c.get_account, h, fields=[c.Account.Fields.POSITIONS])["securitiesAccount"]
    b = a.get("currentBalances", {})
    # settled cash only (no good-faith violations): take the most conservative figure Schwab gives
    cands = [b.get("cashAvailableForTrading"), b.get("cashAvailableForWithdrawal")]
    if b.get("cashBalance") is not None:
        cands.append(b["cashBalance"] - (b.get("unsettledCash") or 0))
    cands = [x for x in cands if x is not None]
    settled = min(cands) if cands else 0.0
    from schwab_api import config as scfg
    excluded = getattr(scfg, "EXCLUDED_SYMBOLS", set())
    excluded_value = 0.0
    pos = {}
    for p in a.get("positions", []):
        i = p["instrument"]
        qty = p.get("longQuantity", 0)
        if i.get("symbol") in excluded:
            excluded_value += p.get("marketValue") or 0.0
            continue
        if qty and i.get("assetType") in ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF"):
            pos[i["symbol"]] = dict(qty=qty, avg=p.get("averagePrice"),
                                    value=p.get("marketValue"),
                                    price=(p.get("marketValue") or 0) / qty)
    return dict(hash=h, last4=last4, type=a.get("type"),
                equity=(b.get("liquidationValue") or 0.0) - excluded_value,
                settled_cash=max(0.0, settled or 0.0), excluded=sorted(excluded),
                cash=b.get("cashBalance"), unsettled=b.get("unsettledCash"),
                positions=pos, balances=b)


def orders(account_hash, days=30):
    c = client()
    return _get(c.get_orders_for_account, account_hash,
                from_entered_datetime=datetime.now() - timedelta(days=days),
                to_entered_datetime=datetime.now() + timedelta(days=1))


def order(account_hash, order_id):
    return _get(client().get_order, order_id, account_hash)


# ── universe ─────────────────────────────────────────────────────────────
def refresh_universe():
    """All US-listed common stocks passing the static filters, plus plain ETFs."""
    r = requests.get("https://api.nasdaq.com/api/screener/stocks",
                     params={"tableonly": "true", "download": "true"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    rows = r.json()["data"]["rows"]
    out = []
    for x in rows:
        sym = (x.get("symbol") or "").strip()
        name = (x.get("name") or "").lower()
        if not sym or not sym.isalpha():
            continue
        try:
            mcap = float(x.get("marketCap") or 0)
            price = float((x.get("lastsale") or "0").replace("$", "").replace(",", ""))
        except ValueError:
            continue
        try:
            vol = float(x.get("volume") or 0)
        except ValueError:
            vol = 0
        if mcap < R.MIN_MCAP or price < R.MIN_PRICE or price * vol < R.MIN_DOLLAR_VOLUME / 5:
            continue
        if any(w in name for w in R.EXCLUDED_NAME_WORDS):
            continue
        sector, industry = x.get("sector") or "Unknown", x.get("industry") or ""
        ipo = int(x["ipoyear"]) if (x.get("ipoyear") or "").isdigit() else None
        # recent IPOs drop out naturally: setups need MIN_HISTORY_BARS (~10 months) of bars
        # binary-event biotech/pharma under $2B: skip entirely
        if sector == "Health Care" and mcap < R.SMALLCAP_MAX_MCAP and \
                any(k in industry for k in ("Biotechnology", "Pharmaceutical")):
            continue
        out.append(dict(symbol=sym, name=x.get("name"), sector=sector, industry=industry,
                        market_cap=mcap, ipo_year=ipo))
    for sym, label in PLAIN_ETFS.items():
        out.append(dict(symbol=sym, name=f"{label} ETF", sector="ETF", industry=label,
                        market_cap=None, ipo_year=None))
    db.save_universe(out)
    return len(out)


# ── Finnhub: earnings + news ─────────────────────────────────────────────
def _finnhub_key():
    from secrets_local import FINNHUB_API_KEY
    return FINNHUB_API_KEY


def earnings_dates(symbol, back=10, fwd=60):
    """Report dates within [today-back, today+fwd]. Empty list if unknown."""
    today = now_et().date()
    try:
        j = requests.get("https://finnhub.io/api/v1/calendar/earnings", timeout=15, params={
            "symbol": symbol, "from": (today - timedelta(days=back)).isoformat(),
            "to": (today + timedelta(days=fwd)).isoformat(), "token": _finnhub_key()}).json()
        time.sleep(1.1)   # free tier: 60/min
        return sorted({date.fromisoformat(e["date"]) for e in j.get("earningsCalendar", [])
                       if e.get("date")})
    except Exception:
        return []


def next_earnings(symbol):
    today = now_et().date()
    fut = [d for d in earnings_dates(symbol, back=0) if d >= today]
    return fut[0] if fut else None


def news(symbol, days=7, limit=8):
    today = now_et().date()
    try:
        j = requests.get("https://finnhub.io/api/v1/company-news", timeout=15, params={
            "symbol": symbol, "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(), "token": _finnhub_key()}).json()
        time.sleep(1.1)
        if not isinstance(j, list):
            return []
        return [dict(date=datetime.fromtimestamp(n["datetime"]).date().isoformat(),
                     headline=n.get("headline", ""), summary=(n.get("summary") or "")[:280],
                     source=n.get("source"))
                for n in sorted(j, key=lambda n: -n.get("datetime", 0))[:limit]]
    except Exception:
        return []
