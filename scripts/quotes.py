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
  Stage    : see stage() - S1 Base / S2 Advance / S3 Top / S4 Decline, only from the TraderLion stage-analysis rules
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
    ("NQ", "NQ=F", "Nasdaq 100 futures", 2, False, None, 12),
    ("ES", "ES=F", "S&P 500 futures", 2, False, None, 12),
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
WIDE_1D = {"SMH": 25, "XBI": 25, "URA": 25, "TAN": 25}   # volatile theme ETFs: data sanity limit for |1D| %, default 15
STRIP = ["QQQ", "NQ", "VIX", "SXR8", "ES", "BTC", "GOLD", "BRENT", "US10Y", "EURUSD"]
PERIODS = [("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126)]
EXT_HOURS = {"QQQ"}          # US-listed: also show the pre-market / after-hours price
NEAR_23H = {"NQ", "ES"}           # ~23h futures: outside US cash hours show the move since the 16:00 ET cash close
US_CASH = {}                # filled by the QQQ fetch: open flag, last cash close, today's open time
MAX_AGE_OPEN = 30 * 60       # seconds; older than this while the market is open = stale
LIVE_BASE = {}               # key -> config + daily closes for the live-price Worker (worker/worker.js)


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
    """Weinstein stage, only from Tradelion rules.md / TraderLion Ultimate Trading Guide (Stage Analysis, pp. 22-32).
    Weekly closes; 30w (150d) and 40w (200d) MAs (rule 2.2). "Rising" / "falling" = this week's MA vs last week's.
      S2 Advance : price above the rising 30w AND the rising 40w          (rule 4.1, guide p. 27)
      S4 Decline : price below the falling 30w AND the falling 40w        (rule 6.1, guide p. 31: Stage 2 flipped)
      otherwise the long-term MAs are flattening / price whips around them (rule 5.4, guide p. 30):
      S3 Top     : if the last stage was S2 or S3   (cycle 2 -> 3 -> 4 -> 1, guide p. 24)
      S1 Base    : if the last stage was S4 or S1
    The stage is walked forward week by week so S3 vs S1 knows what came before."""
    w = close.resample("W").last().dropna()   # calendar weeks, current week = latest price
    if len(w) < 42:
        return ""
    m30, m40 = w.rolling(30).mean(), w.rolling(40).mean()
    st = None
    for i in range(40, len(w)):
        p, a30, a40 = w.iloc[i], m30.iloc[i], m40.iloc[i]
        up = a30 > m30.iloc[i - 1] and a40 > m40.iloc[i - 1]
        dn = a30 < m30.iloc[i - 1] and a40 < m40.iloc[i - 1]
        if p > a30 and p > a40 and up:
            st = "S2"
        elif p < a30 and p < a40 and dn:
            st = "S4"
        elif st in ("S2", "S3"):
            st = "S3"
        elif st in ("S4", "S1"):
            st = "S1"
    return st or ""


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


def since_cash_close(t, dec: int, now):
    """Futures move since the last US cash close (16:00 ET) = clean pre-market / after-hours sentiment.
    Yahoo's futures 1D is vs the prior settlement, which between 16:00 and 18:00 ET still contains the whole cash day.
    Uses 1-minute trades: Yahoo stamps the settlement price on the 16:59 ET bar (CME halt), so that bar is dropped."""
    close = US_CASH["close"]
    df = t.history(period="5d", interval="1m").dropna(subset=["Close"])
    et = df.index.tz_convert("America/New_York")
    df = df[~((et.hour == 16) & (et.minute == 59))]
    secs = df.index.map(lambda i: i.timestamp())
    ref, after = df[secs < close - 59], df[secs >= close]                   # 15:59 bar closes at 16:00
    if ref.empty or after.empty:
        return None
    r, last = float(ref["Close"].iloc[-1]), float(after["Close"].iloc[-1])
    start = US_CASH.get("start")
    return {"session": "pre" if (start and now.timestamp() < start) else "post", "price": round(last, dec), "chg": pct(last, r),
            "ref": round(r, dec), "refTime": datetime.fromtimestamp(close, timezone.utc).isoformat(timespec="minutes"),
            "time": after.index[-1].tz_convert("UTC").isoformat(timespec="minutes")}


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
    # daily closes for the live-price Worker, saved before the checks so a ticker failing this run can still go live later
    d = df.dropna(subset=["Close"]).tail(420)                # 420 days: covers YTD + 6M even for 7-day markets (BTC)
    if len(d):
        LIVE_BASE[key] = {"symbol": sym, "dec": dec, "dot": dot_rule, "max": max_chg,
                          "closes": [[i.strftime("%Y-%m-%d"), round(float(c), dec + 2)] for i, c in zip(d.index.tz_convert(tzname), d["Close"])]}

    # ---- checks ----
    if not (math.isfinite(price) and price > 0 and math.isfinite(prev) and prev > 0):
        raise ValueError(f"bad price {price} / prev {prev}")
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise ValueError("no daily history")
    df.index = df.index.tz_convert(tzname)
    session = pd.Timestamp(as_of, unit="s", tz="UTC").tz_convert(tzname).normalize() if as_of else df.index[-1].normalize()
    done = df[df.index.normalize() < session]                # completed sessions only
    today = df[df.index.normalize() == session]
    warn = []
    if len(done) and abs(done["Close"].iloc[-1] / prev - 1) > 0.01:
        last_done = float(done["Close"].iloc[-1])
        if not sym.endswith("=F"):   # Yahoo's previous-close field sometimes lags a day (seen on ^VIX): the daily close is the truth
            warn.append(f"Yahoo prev close {prev:.{dec}f} ≠ last daily close {last_done:.{dec}f}; using the daily close")
            prev = last_done
        else:
            warn.append(f"previous bar {last_done:.{dec}f} ≠ prev close {prev:.{dec}f} (contract roll / data gap)")
    chg = pct(price, prev)
    if abs(chg) > max_chg:
        raise ValueError(f"1D {chg}% beyond ±{max_chg}%")
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
    if key == "QQQ":
        US_CASH.update(open=open_now, close=as_of, start=start)
    if key in NEAR_23H and US_CASH and not US_CASH["open"] and US_CASH.get("close"):
        try:
            x = since_cash_close(t, dec, now)
            if x:
                rec["eth"] = x
        except Exception as exc:
            print(f"{key} since cash close: {exc}")
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
    chart = {"key": key, "symbol": sym, "dec": dec, "asOf": rec["asOf"], "bars": bars[-800:]}  # ~2 years: 200 SMA spans the daily view, 52 weeks + 30w SMA on the weekly mini charts
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
    universe = MARKETS + [(sym, sym, name, 2, True, "ema", WIDE_1D.get(sym, 15)) for name, sym in SECTORS + THEMES] + [("SPY", "SPY", "S&P 500", 2, True, "ema", 12)]
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
    # live_base.json: what the live-price Worker needs to apply the same rules between builds (daily closes per ticker)
    with open("live_base.json", "w") as fh:
        json.dump({"updated": snap["updated"], "ext": sorted(EXT_HOURS), "near23h": sorted(NEAR_23H), "cash": "QQQ",
                   "maxAgeOpen": MAX_AGE_OPEN, "periods": PERIODS, "tickers": LIVE_BASE}, fh, separators=(",", ":"))
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
    base = previous_site("live_base.json")
    if base:
        with open("live_base.json", "w") as fh:
            json.dump(base, fh, separators=(",", ":"))
    for key in snap.get("quotes", {}):
        chart = previous_site(f"charts/{key}.json")
        if chart:
            with open(f"charts/{key}.json", "w") as fh:
                json.dump(chart, fh, separators=(",", ":"))
    print("restored previous snapshot", snap.get("updated"))
    return 0


if __name__ == "__main__":
    sys.exit(restore() if "--restore" in sys.argv else main())
