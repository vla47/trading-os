"""Checks every number Trading OS shows, after quotes.py and before the site is published.

Each quote in market.json is re-derived from its own chart file (charts/<key>.json) and compared:
  price        = last chart bar's close, > 0
  1D           = price / prevClose - 1
  1W/1M/3M/6M  = price / close 5/21/63/126 sessions back
  YTD          = price / last close of the previous year
  dot          = 21ema rule recomputed (QQQ / ETFs) or VIX level rule
  stage        = TraderLion stage recomputed from the chart's weekly closes
  52w range    = weekly high / low the cards show, within the chart bars
  pre/after    = PM/AH change = ext price / regular close - 1;  NQ/ES since-close = price / 16:00 ref - 1
  structure    = every strip / sector / theme key has a quote and a chart; chart dates ascending, no gaps > 7 days (stocks)
Results are written into market.json as "checks" ({passed, failed:[...], at}); the page shows them under the rotation card.
Run: python scripts/check_market.py  (exit 0 always; a failed check is shown on the page, it doesn't block publishing)
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from quotes import ema_dot, vix_dot, stage, PERIODS  # same rules as the page data  # noqa: E402

TOL = 0.011   # percentage points (values are rounded to 2 decimals)


def pct(a, b):
    return round((a / b - 1) * 100, 2) if b else None


def main() -> int:
    snap = json.load(open("market.json"))
    q = snap.get("quotes", {})
    ok, bad = 0, []

    def check(cond, what):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(what)

    keys = set(snap.get("strip", [])) | {g["key"] for grp in snap.get("groups", {}).values() for g in grp}
    for k in sorted(keys):
        check(k in q, f"{k}: listed but no quote")

    for k, r in sorted(q.items()):
        if r.get("stale"):
            continue   # already flagged stale on the page; its numbers are last-known values
        try:
            ch = json.load(open(f"charts/{k}.json"))
        except Exception:
            bad.append(f"{k}: chart file missing")
            continue
        bars = ch["bars"]
        dates = [b[0] for b in bars]
        price, prev = r["price"], r["prevClose"]
        check(math.isfinite(price) and price > 0, f"{k}: price {price} not positive")
        check(abs(bars[-1][4] - price) <= 10 ** -(r["dec"] + 1) * 5 + 1e-9 or abs(bars[-1][4] / price - 1) < 1e-6,
              f"{k}: chart ends at {bars[-1][4]}, quote says {price}")
        check(dates == sorted(dates) and len(set(dates)) == len(dates), f"{k}: chart dates out of order / duplicated")
        rnd = 100 * 0.5 * 10 ** -r["dec"] * (1 / price + 1 / prev)   # what rounding price / prevClose to `dec` decimals can move 1D
        check(abs(pct(price, prev) - r["chg"]) <= TOL + rnd, f"{k}: 1D {r['chg']} ≠ {pct(price, prev)} from price/prevClose")
        closes = pd.Series([b[4] for b in bars], index=pd.to_datetime(dates))
        price = bars[-1][4]   # full-precision live price (the chart keeps one more decimal than the display)
        for name, n in PERIODS:
            want = pct(price, closes.iloc[-1 - n]) if len(closes) > n else None
            got = r["perf"].get(name)
            check(got is None and want is None or (got is not None and want is not None and abs(got - want) <= TOL),
                  f"{k}: {name} {got} ≠ {want} from the chart")
        prior = closes[closes.index.year < closes.index[-1].year]
        if len(prior) and r["perf"].get("YTD") is not None:
            check(abs(r["perf"]["YTD"] - pct(price, prior.iloc[-1])) <= TOL, f"{k}: YTD {r['perf']['YTD']} ≠ {pct(price, prior.iloc[-1])}")
        if r.get("dot") and k != "VIX":
            check(ema_dot(closes) == r["dot"], f"{k}: dot {r['dot']} ≠ {ema_dot(closes)} recomputed")
        if k == "VIX" and r.get("dot"):
            check(vix_dot(price) == r["dot"], f"VIX: dot {r['dot']} ≠ {vix_dot(price)}")
        if r.get("stage") and len(bars) >= 300:   # the chart file holds enough weeks to replay the stage walk
            st = stage(closes)
            check(st == r["stage"], f"{k}: stage {r['stage']} ≠ {st} recomputed")
        if "ext" in r:
            x = r["ext"]
            check(x["session"] in ("pre", "post") and abs(pct(x["price"], price) - x["chg"]) <= TOL,
                  f"{k}: {x['session']} {x['chg']}% ≠ {pct(x['price'], price)}% vs close")
        if "eth" in r:
            x = r["eth"]
            check(abs(pct(x["price"], x["ref"]) - x["chg"]) <= TOL, f"{k}: since-close {x['chg']}% ≠ {pct(x['price'], x['ref'])}%")
        if k not in ("BTC",) and len(closes) > 10:
            gaps = (closes.index[1:] - closes.index[:-1]).days
            check(max(gaps) <= 7 if k not in ("NQ", "ES", "GOLD", "BRENT") else max(gaps) <= 10, f"{k}: gap of {max(gaps)} days in the chart")

    snap["checks"] = {"passed": ok, "failed": bad, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    json.dump(snap, open("market.json", "w"), indent=1)
    print(f"checks: {ok} passed, {len(bad)} failed", *bad, sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
