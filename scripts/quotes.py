"""Market snapshot for Trading OS: ONE source (Yahoo Finance), fetched ONCE per run, every number computed HERE.

Writes market.json (everything the page shows: strip, Markets / Sectors / Themes bars, chart captions)
and charts/<key>.json (daily bars whose last bar is the same live price). The page only displays; it does no price maths.

Per ticker, one fetch:
  price, prevClose, asOf  <- Yahoo chart meta (regularMarketPrice / chartPreviousClose / regularMarketTime)
  series                  <- completed daily closes before the price's session + the live price as the last value
  1D                      =  price / prevClose - 1           (the same 1D everywhere on the page)
  1W/1M/3M/6M             =  series[-1] / series[-1-n] - 1   (5/21/63/126 sessions)
  YTD                     =  price / last close of the previous year - 1

Checks before publishing (a failed ticker keeps its last good value from the live site, marked stale; it never disappears):
  price finite and > 0 | |1D| within a limit | previous bar close agrees with prevClose (else flagged, futures roll)
  quote older than 30 min while its market is open -> stale

Rules (Tradelion rules.md):
  QQQ / ETF dot (21ema): red below | yellow fresh reclaim (<5 days) or 21ema not rising | green above rising
  VIX dot  : red >25 | yellow 15-25 | green <15
  Stage    (weekly bars): S2 U above rising 30w + 10w rising | S4 D below falling 30w + 10w falling
           mixed: below 30w and >15% lower than 13 weeks ago = S4 D; within 20% of 52w high and 30w not falling = S3 T, else S1 B
"""
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

SITE = "https://vla47.github.io/trading-os/"
SOURCE = "Yahoo Finance"

# key, yahoo symbol, display name, decimals, stage?, dot rule, max |1D| %
MARKETS = [
    ("QQQ", "QQQ", "QQQ", 2, True, "ema", 12),
    ("SXR8", "SXR8.DE", "SXR8", 2, True, None, 12),
    ("BTC", "BTC-USD", "BTC", 0, True, None, 25),
    ("GOLD", "GC=F", "GOLD", 2, True, None, 12),
    ("BRENT", "BZ=F", "BRENT", 2, False, None, 20),
    ("US10Y", "^TNX", "US10Y", 2, False, None, 15),
    ("EURUSD", "EURUSD=X", "EURUSD", 4, False, None, 5),
    ("VIX", "^VIX", "VIX", 2, False, "vix", 80),
]
SECTORS = [  # Finviz sector names -> SPDR sector ETFs
    ("Technology", "XLK"), ("Communication Services", "XLC"), ("Consumer Cyclical", "XLY"),
    ("Consumer Defensive", "XLP"), ("Financial", "XLF"), ("Healthcare", "XLV"), ("Industrials", "XLI"),
    ("Energy", "XLE"), ("Basic Materials", "XLB"), ("Utilities", "XLU"), ("Real Estate", "XLRE"),
]
THEMES = [  # industry / theme ETFs that often lead rotations
    ("Semiconductors", "SMH"), ("Software", "IGV"), ("Biotech", "XBI"), ("Aerospace & Defense", "ITA"),
    ("Uranium & Nuclear", "URA"), ("Gold Miners", "GDX"), ("Homebuilders", "XHB"), ("Regional Banks", "KRE"),
    ("Solar", "TAN"), ("China Internet", "KWEB"),
]
STRIP = ["QQQ", "VIX", "SXR8", "BTC", "GOLD", "BRENT", "US10Y", "EURUSD"]
PERIODS = [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126)]
EXT_HOURS = {"QQQ"}          # US-listed: also show the pre-market / after-hours price
MAX_AGE_OPEN = 30 * 60       # seconds; older than this while the market is open = stale


def ema(s: pd.Series, n: int = 21) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def ema_dot(close: pd.Series) -> str:
    c = close.tail(130)
    e = ema(c)
    if c.iloc[-1] < e.iloc[-1]:
        return "red"
    recent_below = (c.iloc[-6:-1] < e.iloc[-6:-1]).any()
    rising = e.iloc[-1] > e.iloc[-6] * 1.001
    return "yellow" if (recent_below or not rising) else "green"


def vix_dot(v: float) -> str:
    return "red" if v > 25 else "yellow" if v >= 15 else "green"


def stage(close: pd.Series) -> str:
    w = close.resample("W").last().dropna()  # calendar weeks, current week = latest price
    if len(w) < 45:
        return ""
    ma, ma4 = w.iloc[-30:].mean(), w.iloc[-34:-4].mean()
    f, f4 = w.iloc[-10:].mean(), w.iloc[-14:-4].mean()
    hi, p = w.iloc[-52:].max(), w.iloc[-1]
    up, dn = ma > ma4 * 1.005, ma < ma4 * 0.995
    fup, fdn = f > f4 * 1.005, f < f4 * 0.995
    if p >= ma and up and fup:
        return "S2 U"
    if p < ma and dn and fdn:
        return "S4 D"
    if p < ma and len(w) > 13 and w.iloc[-14] > p * 1.15:
        return "S4 D"   # fresh breakdown: >15% lower than 13 weeks ago while the 30w is still turning
    return "S3 T" if (p > hi * 0.80 and ma >= ma4) else "S1 B"


def ts(v):
    """Yahoo meta times come as epoch seconds or Timestamps depending on the yfinance version."""
    if v is None:
        return None
    return float(v) if isinstance(v, (int, float)) else pd.Timestamp(v).timestamp()


def ext_quote(t, reg_price: float, reg: dict, dec: int):
    """Latest pre-market or after-hours trade vs the regular-session price (None inside regular hours)."""
    df = t.history(period="1d", interval="1m", prepost=True)
    if df.empty:
        return None
    start, end = ts(reg.get("start")), ts(reg.get("end"))
    last_ts, last = df.index[-1].timestamp(), float(df["Close"].dropna().iloc[-1])
    if not (start and end) or start <= last_ts < end:
        return None
    return {"session": "pre" if last_ts < start else "post", "price": round(last, dec),
            "chg": round((last / reg_price - 1) * 100, 2),
            "time": datetime.fromtimestamp(last_ts, timezone.utc).isoformat(timespec="minutes")}


def pct(a: float, b: float):
    return round((a / b - 1) * 100, 2) if b else None


def fetch(key, sym, name, dec, want_stage, dot_rule, max_chg, now):
    """One ticker, one source: returns (record, chart) or raises."""
    t = yf.Ticker(sym)
    df = t.history(period="2y", interval="1d", auto_adjust=False)
    t.history(period="1d")                       # refreshes the chart meta to the current session
    meta = t.get_history_metadata()
    price = float(meta.get("regularMarketPrice") or "nan")
    prev = float(meta.get("chartPreviousClose") or "nan")
    as_of = ts(meta.get("regularMarketTime"))
    tzname = meta.get("exchangeTimezoneName") or "UTC"
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}

    # ---- checks ----
    if not (math.isfinite(price) and price > 0 and math.isfinite(prev) and prev > 0):
        raise ValueError(f"bad price {price} / prev {prev}")
    chg = pct(price, prev)
    if abs(chg) > max_chg:
        raise ValueError(f"1D {chg}% beyond ±{max_chg}%")
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise ValueError("no daily history")
    df.index = df.index.tz_convert(tzname)
    session = pd.Timestamp(as_of, unit="s", tz="UTC").tz_convert(tzname).normalize() if as_of else df.index[-1].normalize()
    done = df[df.index.normalize() < session]                # completed sessions only
    today = df[df.index.normalize() == session]
    warn = []
    if len(done) and abs(done["Close"].iloc[-1] / prev - 1) > 0.01:
        warn.append(f"previous bar {done['Close'].iloc[-1]:.{dec}f} ≠ prev close {prev:.{dec}f} (contract roll / data gap)")
    start, end = ts(reg.get("start")), ts(reg.get("end"))
    open_now = bool(start and end and start <= now.timestamp() < end)
    age = now.timestamp() - as_of if as_of else None
    stale = bool(open_now and age is not None and age > MAX_AGE_OPEN)
    if stale:
        warn.append(f"quote {int(age // 60)} min old while the market is open")

    # ---- one series: completed closes + live price ----
    s = pd.concat([done["Close"], pd.Series([price], index=[session])])
    s.index = s.index.tz_localize(None)
    perf = {"1D": chg}
    for k, n in PERIODS:
        perf[k] = pct(price, float(s.iloc[-1 - n])) if len(s) > n else None
    prev_year = s[s.index.year < s.index[-1].year]
    perf["YTD"] = pct(price, float(prev_year.iloc[-1])) if len(prev_year) else None

    rec = {"key": key, "name": name, "symbol": sym, "dec": dec, "price": round(price, dec), "prevClose": round(prev, dec),
           "chg": chg, "perf": perf, "asOf": datetime.fromtimestamp(as_of, timezone.utc).isoformat(timespec="seconds") if as_of else None,
           "open": open_now, "stale": stale, "source": SOURCE}
    if warn:
        rec["warn"] = warn
    if dot_rule == "ema":
        rec["dot"] = ema_dot(s)
    elif dot_rule == "vix":
        rec["dot"] = vix_dot(price)
    if want_stage:
        rec["stage"] = stage(s)
    if key in EXT_HOURS:
        try:
            x = ext_quote(t, price, reg, dec)
            if x:
                rec["ext"] = x
        except Exception as exc:
            print(f"{key} extended hours: {exc}")

    # ---- chart: same completed bars + today's bar closing at the same live price ----
    r = lambda v: round(float(v), dec + 1 if dec < 4 else dec)
    bars = [[i.strftime("%Y-%m-%d"), r(o), r(h), r(lo), r(c), int(v) if v == v else 0]
            for i, o, h, lo, c, v in zip(done.index, done["Open"], done["High"], done["Low"], done["Close"], done["Volume"])]
    if len(today):
        o, h, lo, v = today["Open"].iloc[-1], today["High"].iloc[-1], today["Low"].iloc[-1], today["Volume"].iloc[-1]
    else:
        o, h, lo, v = prev, price, price, 0
    bars.append([session.strftime("%Y-%m-%d"), r(o), r(max(h, price)), r(min(lo, price)), r(price), int(v) if v == v else 0])
    chart = {"key": key, "symbol": sym, "dec": dec, "asOf": rec["asOf"], "bars": bars[-400:]}  # 400 bars: 200 SMA spans the view
    return rec, chart


def previous_site(path):
    try:
        with urllib.request.urlopen(SITE + path, timeout=20) as fh:
            return json.load(fh)
    except Exception:
        return None


def add_rotation(keys, quotes):
    """Rank change: 1M rank vs 3M rank. Positive = moving up the table (money rotating in)."""
    val = lambda k, p: quotes[k]["perf"].get(p) if quotes[k]["perf"].get(p) is not None else -999
    r1 = {k: i for i, k in enumerate(sorted(keys, key=lambda k: -val(k, "1M")))}
    r3 = {k: i for i, k in enumerate(sorted(keys, key=lambda k: -val(k, "3M")))}
    return [{"key": k, "rot": r3[k] - r1[k]} for k in keys]


def main() -> int:
    now = datetime.now(timezone.utc)
    prev_snap = previous_site("market.json") or {}
    prev_quotes = prev_snap.get("quotes", {})
    os.makedirs("charts", exist_ok=True)
    universe = MARKETS + [(sym, sym, name, 2, True, "ema", 15) for name, sym in SECTORS + THEMES] + [("SPY", "SPY", "S&P 500", 2, True, "ema", 12)]
    quotes, fresh, failed = {}, 0, []
    for key, sym, name, dec, want_stage, dot_rule, max_chg in universe:
        try:
            rec, chart = fetch(key, sym, name, dec, want_stage, dot_rule, max_chg, now)
            with open(f"charts/{key}.json", "w") as fh:
                json.dump(chart, fh, separators=(",", ":"))
            quotes[key] = rec
            fresh += 0 if rec["stale"] else 1
        except Exception as exc:
            failed.append(f"{key}: {exc}")
            old = prev_quotes.get(key)
            if old:  # keep the last good value, clearly marked; its chart from the live site
                old = {**old, "stale": True, "warn": [f"not refreshed: {exc}"]}
                quotes[key] = old
                chart = previous_site(f"charts/{key}.json")
                if chart:
                    with open(f"charts/{key}.json", "w") as fh:
                        json.dump(chart, fh, separators=(",", ":"))
    if fresh == 0:
        print("no fresh quotes at all:", *failed, sep="\n  ")
        return 1
    snap = {
        "version": 2, "updated": now.isoformat(timespec="seconds"), "source": SOURCE,
        "strip": [k for k in STRIP if k in quotes],
        "groups": {
            "markets": [{"key": k} for k, *_ in MARKETS if k in quotes],
            "sectors": add_rotation([s for _, s in SECTORS if s in quotes], quotes),
            "themes": add_rotation([s for _, s in THEMES if s in quotes], quotes),
        },
        "benchmark": "SPY" if "SPY" in quotes else None,
        "quotes": quotes,
        "failed": failed,
    }
    with open("market.json", "w") as fh:
        json.dump(snap, fh, indent=1)
    print(f"quotes: {len(quotes)} ({fresh} fresh)  failed: {len(failed)}", *failed, sep="\n  ")
    return 0


def restore() -> int:
    """Whole run failed: republish the live site's last snapshot and charts unchanged (marked stale by age on the page)."""
    snap = previous_site("market.json")
    if not snap:
        return 1
    os.makedirs("charts", exist_ok=True)
    with open("market.json", "w") as fh:
        json.dump(snap, fh, indent=1)
    for key in snap.get("quotes", {}):
        chart = previous_site(f"charts/{key}.json")
        if chart:
            with open(f"charts/{key}.json", "w") as fh:
                json.dump(chart, fh, separators=(",", ":"))
    print("restored previous snapshot", snap.get("updated"))
    return 0


if __name__ == "__main__":
    sys.exit(restore() if "--restore" in sys.argv else main())
