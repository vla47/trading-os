"""Market strip for Trading OS: same tickers and rules as the SwiftBar menu-bar plugin.

Writes quotes.json next to index.html. Run by .github/workflows/quotes.yml.

Rules (Tradelion rules.md):
  QQQ dot  (21.2-21.4, daily 21ema): red below | yellow fresh reclaim (<5 days) or 21ema not rising | green above rising
  VIX dot  : red >25 | yellow 15-25 | green <15
  Stage    (2-6, weekly bars): S2 U above rising 30w + 10w rising | S4 D below falling 30w + 10w falling
             mixed: within 20% of 52w high and 30w not falling = S3 T, else S1 B  (rising/falling = >0.5% over 4 weeks)
"""
import json
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
    return "S3 T" if (p > hi * 0.80 and ma >= ma4) else "S1 B"


def vix_dot(v: float) -> str:
    return "red" if v > 25 else "yellow" if v >= 15 else "green"


def main() -> None:
    items = []
    for label, sym, dec, want_stage in TICKERS:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="2y", interval="1d", auto_adjust=False)["Close"].dropna()
            if hist.empty:
                continue
            hist.index = hist.index.tz_localize(None)
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
        except Exception as exc:  # one bad ticker must not kill the strip
            print(f"{label}: {exc}")
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}
    with open("quotes.json", "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
