"""Market strip for Trading OS: same tickers and rules as the SwiftBar menu-bar plugin.

Writes quotes.json, sectors.json and charts/<label>.json next to index.html. Run by .github/workflows/quotes.yml, which deploys it with the site (not committed).

Rules (Tradelion rules.md):
  QQQ dot  (21.2-21.4, daily 21ema): red below | yellow fresh reclaim (<5 days) or 21ema not rising | green above rising
  VIX dot  : red >25 | yellow 15-25 | green <15
  Stage    (2-6, weekly bars): S2 U above rising 30w + 10w rising | S4 D below falling 30w + 10w falling
             mixed: below 30w and >15% lower than 13 weeks ago = S4 D (fresh breakdown);
             within 20% of 52w high and 30w not falling = S3 T, else S1 B  (rising/falling = >0.5% over 4 weeks)
"""
import json
import os
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

TICKERS = [  # (label, yahoo symbol, decimals, stage?)
    ("QQQ", "QQQ", 2, True),
    ("SXR8", "SXR8.DE", 2, True),
    ("BTC", "BTC-USD", 0, True),
    ("GOLD", "GC=F", 2, True),
    ("BRENT", "BZ=F", 2, False),
    ("US10Y", "^TNX", 2, False),
    ("EURUSD", "EURUSD=X", 4, False),
    ("VIX", "^VIX", 2, False),
]


def ema(s: pd.Series, n: int = 21) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def qqq_dot(close: pd.Series) -> str:
    c = close.tail(130)
    e = ema(c)
    if c.iloc[-1] < e.iloc[-1]:
        return "red"
    recent_below = (c.iloc[-6:-1] < e.iloc[-6:-1]).any()
    rising = e.iloc[-1] > e.iloc[-6] * 1.001
    return "yellow" if (recent_below or not rising) else "green"


def stage(close: pd.Series) -> str:
    w = close.resample("W").last().dropna()  # calendar weeks, current week = latest price
    if len(w) < 45:
        return ""
    ma = w.iloc[-30:].mean()
    ma4 = w.iloc[-34:-4].mean()
    f = w.iloc[-10:].mean()
    f4 = w.iloc[-14:-4].mean()
    hi = w.iloc[-52:].max()
    p = w.iloc[-1]
    up, dn = ma > ma4 * 1.005, ma < ma4 * 0.995
    fup, fdn = f > f4 * 1.005, f < f4 * 0.995
    if p >= ma and up and fup:
        return "S2 U"
    if p < ma and dn and fdn:
        return "S4 D"
    if p < ma and len(w) > 13 and w.iloc[-14] > p * 1.15:
        return "S4 D"   # fresh breakdown: >15% lower than 13 weeks ago while the 30w is still turning
    return "S3 T" if (p > hi * 0.80 and ma >= ma4) else "S1 B"


def vix_dot(v: float) -> str:
    return "red" if v > 25 else "yellow" if v >= 15 else "green"


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
PERIODS = [("1D", 1), ("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126)]


def ohlcv_rows(df: pd.DataFrame, dec: int) -> list:
    """Last ~1.6 years of daily bars for the chart: [date, open, high, low, close, volume]."""
    d = df.dropna(subset=["Close"]).tail(400)  # 400 bars so the 200 SMA spans the whole visible range
    r = lambda v: round(float(v), dec + 1 if dec < 4 else dec)
    return [[i.strftime("%Y-%m-%d"), r(o), r(h), r(lo), r(c), int(v) if v == v else 0]
            for i, o, h, lo, c, v in zip(d.index, d["Open"], d["High"], d["Low"], d["Close"], d["Volume"])]


def write_chart(label: str, sym: str, df: pd.DataFrame, dec: int) -> None:
    os.makedirs("charts", exist_ok=True)
    with open(f"charts/{label}.json", "w") as fh:
        json.dump({"label": label, "symbol": sym, "dec": dec, "bars": ohlcv_rows(df, dec)}, fh, separators=(",", ":"))


def perf_row(name: str, sym: str, close: pd.Series) -> dict:
    c = close.dropna()
    row = {"name": name, "symbol": sym, "price": round(float(c.iloc[-1]), 2)}
    for key, n in PERIODS:
        row[key] = round((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2) if len(c) > n else None
    prev_year = c[c.index.year < c.index[-1].year]
    row["YTD"] = round((c.iloc[-1] / prev_year.iloc[-1] - 1) * 100, 2) if len(prev_year) else None
    row["dot"] = qqq_dot(c)          # same 21ema rule as QQQ
    row["stage"] = stage(c)
    return row


def add_rotation(rows: list) -> None:
    """Rank change: 1M rank vs 3M rank. Positive = moving up the table (money rotating in)."""
    r1 = {r["symbol"]: i for i, r in enumerate(sorted(rows, key=lambda r: -(r["1M"] if r["1M"] is not None else -999)))}
    r3 = {r["symbol"]: i for i, r in enumerate(sorted(rows, key=lambda r: -(r["3M"] if r["3M"] is not None else -999)))}
    for r in rows:
        r["rot"] = r3[r["symbol"]] - r1[r["symbol"]]


def history(sym: str) -> pd.DataFrame:
    df = yf.Ticker(sym).history(period="2y", interval="1d", auto_adjust=False)
    df.index = df.index.tz_localize(None)
    return df


def main() -> None:
    items, markets = [], []
    for label, sym, dec, want_stage in TICKERS:
        try:
            t = yf.Ticker(sym)
            df = t.history(period="2y", interval="1d", auto_adjust=False)
            hist = df["Close"].dropna()
            if hist.empty:
                continue
            hist.index = hist.index.tz_localize(None); df.index = df.index.tz_localize(None)
            # live price + previous session close from Yahoo's chart meta (same source as the menu bar;
            # avoids futures roll gaps in the daily history, e.g. Brent)
            t.history(period="1d")
            meta = t.get_history_metadata()
            price = float(meta.get("regularMarketPrice") or hist.iloc[-1])
            prev = float(meta.get("chartPreviousClose") or hist.iloc[-2])
            hist.iloc[-1] = price  # live price as today's bar
            it = {"label": label, "price": round(price, dec), "dec": dec,
                  "chg": round((price / prev - 1) * 100, 2) if prev else None}
            if label == "QQQ":
                it["dot"] = qqq_dot(hist)
            if label == "VIX":
                it["dot"] = vix_dot(price)
            if want_stage:
                it["stage"] = stage(hist)
            items.append(it)
            write_chart(label, sym, df, dec)
            row = perf_row(label, sym, hist)
            row["1D"] = it["chg"]                       # same day change as the strip (chart meta)
            row["dot"] = it.get("dot")                  # QQQ 21ema / VIX level only
            row["stage"] = it.get("stage")
            row["chart"] = label
            markets.append(row)
        except Exception as exc:  # one bad ticker must not kill the strip
            print(f"{label}: {exc}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open("quotes.json", "w") as fh:
        json.dump({"updated": now, "items": items}, fh, indent=1)

    groups = {"markets": markets}
    for gname, lst in (("sectors", SECTORS), ("themes", THEMES), ("benchmark", [("S&P 500", "SPY")])):
        rows = []
        for name, sym in lst:
            try:
                df = history(sym)
                if df.empty:
                    continue
                rows.append(perf_row(name, sym, df["Close"]))
                write_chart(sym, sym, df, 2)
            except Exception as exc:
                print(f"{sym}: {exc}")
        if gname != "benchmark":
            add_rotation(rows)
        groups[gname] = rows
    with open("sectors.json", "w") as fh:
        json.dump({"updated": now, **groups}, fh, indent=1)
    print(f"quotes: {len(items)}  sectors: {len(groups['sectors'])}  themes: {len(groups['themes'])}")


if __name__ == "__main__":
    main()
