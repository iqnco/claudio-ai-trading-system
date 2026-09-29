"""Pure indicator math on a daily OHLCV DataFrame (index = date)."""
import numpy as np
import pandas as pd


def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def atr(df, n=14):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def rsi(s, n=2):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def ret(s, n):
    return s / s.shift(n) - 1


def enrich(df):
    """Add the columns every setup needs. Returns a copy."""
    d = df.copy()
    c = d["close"]
    d["ma5"], d["ma50"], d["ma200"] = sma(c, 5), sma(c, 50), sma(c, 200)
    d["atr"] = atr(d)
    d["rsi2"] = rsi(c, 2)
    d["vol50"] = sma(d["volume"], 50)
    d["dollar_vol50"] = sma(c * d["volume"], 50)
    d["high20_prev"] = d["high"].rolling(20).max().shift(1)   # breakout level excludes today
    d["high10"] = d["high"].rolling(10).max()
    d["low10"] = d["low"].rolling(10).min()
    d["low20"] = d["low"].rolling(20).min()
    d["ret63"] = ret(c, 63)
    d["high252"] = d["high"].rolling(252, min_periods=200).max()
    return d


def rsi2_with_price(closes, price):
    """RSI(2) as if `price` were today's close (for near-close intraday checks)."""
    s = pd.concat([closes, pd.Series([price])], ignore_index=True)
    return float(rsi(s, 2).iloc[-1])
