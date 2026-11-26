"""Leaders list for Trading OS: Vla's filter, one data source (Polygon / Massive grouped daily bars), computed HERE.

Filter (all must pass):
  1. Price >= $5                                          Trading plan §2
  2. 20-day average dollar volume >= $10M                 Trading plan §2
  3. ADR% (20-day) >= 5%                                  Trading plan §2
  4. Top 2% of all US stocks on 1M, 3M or 6M AND >= +30%  Trading plan §2 / Qullamaggie (C/C21, C/C63, C/C126)
  5. Price > 10 EMA > 20 EMA > 50 EMA, price > 200 EMA    real-leaders-scan (Vla's EMA stack)
  6. RS line (close / SPY) within 10% of its 250-bar high "Vla EMAs S/R RS" LEADER flag
Order: RS Rating 1-99 (Fred6724 score 40% 3M + 20% 6M/9M/12M vs SPY, percentile across all US stocks).
Label only (nothing removed): fundamentals tier Real leader / Leader w/ caveat / Not backed (real-leaders-scan rules).

Data (cached on the `data` branch, see .github/workflows/leaders.yml):
  history.parquet  daily OHLCV of US common stocks + ADRs (Nasdaq/NYSE/AMEX) and SPY, ~300 sessions
  tickers.json     universe from Polygon reference tickers (refreshed weekly)
  state.json       holidays seen, date each session was fetched, splits applied
  fund.json        fundamentals per ticker (refreshed weekly)
  leaders.json     what the page shows
  lc/<T>.json      daily bars of each listed stock for the page's candle chart (rebuilt every run)

Checks before publishing: SPY and >= 90% of the previous session's stocks have the latest bar, latest bar no older than
4 days. If a check fails the previous rows are kept and the file is marked stale with the reason.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

API = "https://api.polygon.io"
KEY = os.environ.get("POLYGON_API_KEY", "")
GAP = 12.6            # free tier: 5 requests / minute
SESSIONS = 300        # enough for 252-bar RS Rating + EMA200 warm-up
EXCH = {"XNAS", "XNYS", "XASE", "ARCX", "BATS"}
RULES = [
    ["Price ≥ $5", "Trading plan §2"],
    ["20-day avg $ volume ≥ $10M", "Trading plan §2"],
    ["ADR (20-day) ≥ 5%", "Trading plan §2"],
    ["Top 2% of all US stocks on 1M, 3M or 6M and ≥ +30% (close vs close 21/63/126 days ago)", "Trading plan §2 · Qullamaggie"],
    ["Price > 10 EMA > 20 EMA > 50 EMA, price > 200 EMA", "real-leaders-scan"],
    ["RS line (stock ÷ SPY) within 10% of its 1-year high", "Vla EMAs S/R RS · LEADER"],
    ["Sorted by RS Rating 1–99 (Fred6724 method)", "Vla EMAs S/R RS"],
    ["Tier: revenue YoY ≥ 25%, revenue growth speeding up or strong, EPS up YoY, mcap ≥ $5B, $vol ≥ $75M", "real-leaders-scan"],
]

_last = [0.0]


def poly(path, **params):
    """GET a Polygon endpoint, spaced for the free tier, retried on 429 / network errors."""
    params["apiKey"] = KEY
    url = path if path.startswith("http") else API + path
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in range(6):
        wait = _last[0] + GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(30 * (attempt + 1))
                continue
            raise
        except Exception:
            time.sleep(20 * (attempt + 1))
    raise RuntimeError("Polygon unreachable: " + path)


def jload(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def jsave(p, obj):
    with open(p, "w") as f:
        json.dump(obj, f, separators=(",", ":"), default=lambda x: None)


# ---------------------------------------------------------------- universe
def universe(d, state):
    path = os.path.join(d, "tickers.json")
    cur = jload(path, {})
    if cur.get("at", "") > (date.today() - timedelta(days=7)).isoformat() and cur.get("tickers"):
        return cur["tickers"]
    out = {}
    for typ in ("CS", "ADRC"):
        j = poly("/v3/reference/tickers", market="stocks", type=typ, active="true", limit=1000)
        while True:
            for r in j.get("results", []):
                if r.get("primary_exchange") in EXCH:
                    out[r["ticker"]] = r.get("name", "")
            nxt = j.get("next_url")
            if not nxt:
                break
            j = poly(nxt)
    if len(out) < 3000:
        raise RuntimeError(f"universe too small ({len(out)})")
    jsave(path, {"at": date.today().isoformat(), "tickers": out})
    print("universe refreshed:", len(out))
    return out


# ---------------------------------------------------------------- history
def weekdays_back(n_cal):
    t = date.today()
    return [t - timedelta(days=i) for i in range(n_cal, -1, -1) if (t - timedelta(days=i)).weekday() < 5]


def update_history(d, state, names):
    path = os.path.join(d, "history.parquet")
    hist = pd.read_parquet(path) if os.path.exists(path) else pd.DataFrame(columns=["d", "T", "o", "h", "l", "c", "v"])
    have = set(hist["d"].unique()) if len(hist) else set()
    holidays = set(state.get("holidays", []))
    fetched = state.setdefault("fetched", {})
    keep = set(names) | {"SPY"}
    today = date.today()
    want = [x.isoformat() for x in weekdays_back(int(SESSIONS * 7 / 5) + 20)]
    missing = [x for x in want if x not in have and x not in holidays]
    print(f"history: {len(have)} sessions cached, fetching {len(missing)}")
    new = []
    def save():                           # checkpoint: a later failure never loses the sessions already fetched
        h = pd.concat([hist] + new, ignore_index=True) if new else hist
        for c in "ohlcv":
            h[c] = h[c].astype("float32")
        h.to_parquet(path, index=False)
        state["holidays"] = sorted(holidays)
        jsave(os.path.join(d, "state.json"), state)
    for i, ds in enumerate(missing):
        try:
            j = poly(f"/v2/aggs/grouped/locale/us/market/stocks/{ds}", adjusted="true")
        except urllib.error.HTTPError as e:
            # free plan: a session is published after its end of day (403 NOT_AUTHORIZED before that)
            if e.code == 403 and date.fromisoformat(ds) >= today - timedelta(days=1):
                print(f"  {ds} not published yet, next run")
                continue
            save()
            raise
        rows = [r for r in j.get("results") or [] if r.get("T") in keep and r.get("c")]
        if not rows:
            if date.fromisoformat(ds) < today - timedelta(days=3):
                holidays.add(ds)          # market closed that day
            continue                      # recent day: not published yet, try next run
        df = pd.DataFrame(rows)[["T", "o", "h", "l", "c", "v"]]
        df["d"] = ds
        new.append(df)
        fetched[ds] = today.isoformat()   # Polygon adjusts bars for splits up to this date
        if i % 25 == 0:
            print(f"  {i + 1}/{len(missing)} {ds} {len(df)} stocks", flush=True)
            save()
    if new:
        hist = pd.concat([hist] + new, ignore_index=True)
    hist = apply_splits(hist, state)
    days = sorted(hist["d"].unique())[-SESSIONS:]
    hist = hist[hist["d"].isin(days)]
    state["fetched"] = {k: v for k, v in fetched.items() if k in set(days)}
    for c in "ohlcv":
        hist[c] = hist[c].astype("float32")
    hist.to_parquet(path, index=False)
    state["holidays"] = sorted(h for h in holidays if h >= days[0])
    return hist


def apply_splits(hist, state):
    """Rescale bars that are on the old share basis.

    Bars come with adjusted=true, so a bar is already on the new basis if it was fetched on or after the split's
    execution date. Only bars fetched BEFORE the split are rescaled (state["fetched"] = session -> fetch date).
    """
    if not len(hist):
        return hist
    done = set(state.get("splits", []))
    fetched = state.setdefault("fetched", {})
    if state.get("splits_fix") != 1:
        hist = undo_double_splits(hist, state, done, fetched)
    first = min(hist["d"])
    j = poly("/v3/reference/splits", **{"execution_date.gte": (date.today() - timedelta(days=30)).isoformat(), "limit": 1000})
    for s in j.get("results", []):
        sid, t, ex = s.get("id"), s.get("ticker"), s.get("execution_date", "")
        if sid in done or ex > date.today().isoformat() or ex <= first or not s.get("split_from") or not s.get("split_to"):
            continue
        k = s["split_from"] / s["split_to"]          # 1-for-2 split: old prices x 0.5
        old = {d for d, f in fetched.items() if d < ex and f < ex}
        m = (hist["T"] == t) & hist["d"].isin(old)
        if m.any():
            hist.loc[m, ["o", "h", "l", "c"]] *= k
            hist.loc[m, "v"] /= k
        done.add(sid)
    state["splits"] = sorted(done)[-2000:]
    return hist


def undo_double_splits(hist, state, done, fetched):
    """One-time repair (Nov 2026): the first full load fetched every session on 2026-11-25, already split-adjusted,
    and the old code rescaled the bars again for each split since 2026-10-26. Undo those rescales once."""
    load = "2026-11-25"
    for d in hist["d"].unique():
        fetched.setdefault(d, load)
    j = poly("/v3/reference/splits", **{"execution_date.gte": "2026-10-01", "limit": 1000})
    n = 0
    for s in j.get("results", []):
        sid, t, ex = s.get("id"), s.get("ticker"), s.get("execution_date", "")
        if sid not in done or not s.get("split_from") or not s.get("split_to") or ex > load:
            continue
        k = s["split_from"] / s["split_to"]
        m = (hist["T"] == t) & (hist["d"] < ex)
        if m.any():
            hist.loc[m, ["o", "h", "l", "c"]] /= k
            hist.loc[m, "v"] *= k
            n += 1
    print(f"split repair: undid {n} double adjustments")
    state["splits_fix"] = 1
    return hist


# ---------------------------------------------------------------- fundamentals (real-leaders-scan rules)
def fundamentals(d, tickers):
    path = os.path.join(d, "fund.json")
    cache = jload(path, {})
    fresh = (date.today() - timedelta(days=7)).isoformat()
    todo = [t for t in tickers if cache.get(t, {}).get("at", "") < fresh]
    if todo:
        import yfinance as yf

        def one(t):
            out = {"at": date.today().isoformat()}
            try:
                tk = yf.Ticker(t.replace(".", "-"))
                try:
                    out["mcap"] = float(tk.fast_info["market_cap"])
                except Exception:
                    pass
                q = tk.quarterly_income_stmt
                rv = q.loc["Total Revenue"].sort_index().dropna()
                ep = q.loc["Diluted EPS"].sort_index() if "Diluted EPS" in q.index else pd.Series(dtype=float)
                pc = lambda a, b: None if not b or pd.isna(b) else float((a / b - 1) * 100)
                out.update(lastQ=str(rv.index[-1].date()),
                           revYoY=pc(rv.iloc[-1], rv.iloc[-5]) if len(rv) >= 5 else None,
                           qoq=pc(rv.iloc[-1], rv.iloc[-2]) if len(rv) >= 2 else None,
                           qoqPrev=pc(rv.iloc[-2], rv.iloc[-3]) if len(rv) >= 3 else None,
                           eps=float(ep.iloc[-1]) if len(ep) and pd.notna(ep.iloc[-1]) else None,
                           epsYrAgo=float(ep.iloc[-5]) if len(ep) >= 5 and pd.notna(ep.iloc[-5]) else None)
            except Exception as e:
                out["err"] = str(e)[:80]
            return t, out

        with ThreadPoolExecutor(4) as ex:
            for t, v in ex.map(one, todo):
                if "err" in v and t in cache:      # keep the last good numbers
                    continue
                cache[t] = v
        jsave(path, cache)
    return cache


def tier(f, dv):
    r = f.get("revYoY")
    if r is None or r < 25:
        return "Not backed", "revenue YoY < 25%" if r is not None else "no revenue data"
    q, qp = f.get("qoq"), f.get("qoqPrev")
    accel = (q if q is not None else -99) >= (qp if qp is not None else -99) or ((q or 0) >= 8 and (qp or 0) >= 8)
    eps, eps0 = f.get("eps"), f.get("epsYrAgo")
    eps_ok = eps is not None and eps0 is not None and eps > eps0
    big = (f.get("mcap") or 0) >= 5e9 and dv >= 75
    miss = [n for n, ok in (("revenue growth slowing", accel), ("EPS not up YoY", eps_ok), ("size < $5B / $75M", big)) if not ok]
    return ("Real leader", "") if not miss else ("Leader w/ caveat", ", ".join(miss))


# ---------------------------------------------------------------- scan
def scan(hist, names):
    W = lambda col: hist.pivot(index="d", columns="T", values=col).sort_index()
    C, H, L, V, O = W("c"), W("h"), W("l"), W("v"), W("o")
    traded = C.iloc[-1].notna()            # must have a bar on the latest session
    C = C.ffill(limit=5)
    spy = C["SPY"].dropna()
    last = C.index[-1]
    live = [t for t in C.columns if t != "SPY" and t in names and traded[t]]
    Cl = C[live]

    # 4. momentum: rank ALL stocks, close vs close 21/63/126 sessions ago
    P = pd.DataFrame({k: (Cl.iloc[-1] / Cl.iloc[-1 - n] - 1) * 100 for k, n in (("1M", 21), ("3M", 63), ("6M", 126))})
    R = P.rank(pct=True)
    mom = ((R >= 0.98) & (P >= 30)).any(axis=1)

    # RS Rating 1-99: Fred6724 score (as in the indicator: lookback capped at the bars available), percentile across all
    def wscore(c):
        c = c.dropna()
        if len(c) < 2:
            return np.nan
        g = lambda n: c.iloc[-1 - min(n, len(c) - 1)]
        v = c.iloc[-1]
        return 0.4 * v / g(63) + 0.2 * v / g(126) + 0.2 * v / g(189) + 0.2 * v / g(252)
    ref = wscore(spy)
    rss = pd.Series({t: wscore(Cl[t]) / ref * 100 for t in live})
    rsr = np.clip(np.ceil(rss.rank(pct=True) * 99), 1, 99)

    rows = []
    for t in P.index[mom]:
        c = C[t].dropna()
        p = float(c.iloc[-1])
        e10, e20, e50, e200 = [float(c.ewm(span=n, adjust=False).mean().iloc[-1]) for n in (10, 20, 50, 200)]
        adr = float(((H[t] / L[t]).dropna().iloc[-20:].mean() - 1) * 100)
        dv = float((C[t] * V[t]).dropna().iloc[-20:].mean() / 1e6)
        rs = (C[t] / spy).dropna()
        rsoff = float((rs.iloc[-1] / rs.iloc[-250:].max() - 1) * 100)
        if not (p >= 5 and dv >= 10 and adr >= 5 and p > e10 > e20 > e50 and p > e200 and rsoff >= -10):
            continue
        gap = (O[t] / C[t].shift(1) - 1).iloc[-63:] * 100
        vx = (V[t] / V[t].rolling(50, min_periods=20).mean()).iloc[-63:]
        epd = gap[(gap >= 10) & (vx >= 2)]
        rows.append(dict(t=t, name=names.get(t, ""), rsr=int(rsr[t]), price=round(p, 2), d1=r1((p / c.iloc[-2] - 1) * 100),
                         p1=r1(P.at[t, "1M"]), p3=r1(P.at[t, "3M"]), p6=r1(P.at[t, "6M"]),
                         adr=round(adr, 1), dv=round(dv, 1), rsoff=round(rsoff, 1),
                         offhi=round(float((p / c.iloc[-252:].max() - 1) * 100), 1),
                         ep=str(epd.index[-1]) if len(epd) else None, bars=len(c)))
    return last, len(live), int(mom.sum()), rows


def write_charts(d, hist, tickers):
    """Candle chart data for the page, same bars the filter used: lc/<T>.json = {symbol, dec, bars: [[d,o,h,l,c,v]]}."""
    out = os.path.join(d, "lc")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        os.remove(os.path.join(out, f))
    sub = hist[hist["T"].isin(tickers)].sort_values("d")
    for t, g in sub.groupby("T"):
        bars = [[r.d, round(float(r.o), 2), round(float(r.h), 2), round(float(r.l), 2), round(float(r.c), 2), int(r.v)]
                for r in g.itertuples() if r.c == r.c]
        jsave(os.path.join(out, t + ".json"), {"key": t, "symbol": t, "dec": 2, "bars": bars})


def r1(x):
    return None if pd.isna(x) else round(float(x), 1)


def checks(hist, last):
    per_day = hist.groupby("d")["T"].nunique().sort_index()
    out = []
    if "SPY" not in set(hist.loc[hist["d"] == last, "T"]):
        out.append("SPY missing on " + last)
    if len(per_day) > 1 and per_day.iloc[-1] < 0.9 * per_day.iloc[-2]:
        out.append(f"only {per_day.iloc[-1]} stocks on {last} vs {per_day.iloc[-2]} the session before")
    if date.fromisoformat(last) < date.today() - timedelta(days=4):
        out.append("latest bar " + last + " is more than 4 days old")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--no-fetch", action="store_true", help="use cached history only (testing)")
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    out_path = os.path.join(a.data, "leaders.json")
    prev = jload(out_path, {})
    state = jload(os.path.join(a.data, "state.json"), {})
    try:
        if not KEY and not a.no_fetch:
            raise RuntimeError("POLYGON_API_KEY not set")
        if a.no_fetch:
            names = jload(os.path.join(a.data, "tickers.json"), {}).get("tickers", {})
            hist = pd.read_parquet(os.path.join(a.data, "history.parquet"))
        else:
            names = universe(a.data, state)
            hist = update_history(a.data, state, names)
        jsave(os.path.join(a.data, "state.json"), state)
        last = max(hist["d"])
        bad = checks(hist, last)
        if bad:
            raise RuntimeError("; ".join(bad))
        last, n_live, n_mom, rows = scan(hist, names)
        fund = fundamentals(a.data, [r["t"] for r in rows])
        for r in rows:
            f = fund.get(r["t"], {})
            r["tier"], r["why"] = tier(f, r["dv"])
            r.update(revYoY=r1(f.get("revYoY")), qoq=r1(f.get("qoq")), qoqPrev=r1(f.get("qoqPrev")),
                     eps=f.get("eps"), epsYrAgo=f.get("epsYrAgo"),
                     mcapB=round(f["mcap"] / 1e9, 1) if f.get("mcap") else None, lastQ=f.get("lastQ"))
        rows.sort(key=lambda r: (-r["rsr"], -(r["p3"] or 0)))
        write_charts(a.data, hist, [r["t"] for r in rows])
        res = {"status": "ok", "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "asof": last,
               "source": "Polygon.io (Massive) daily bars · fundamentals Yahoo Finance",
               "counts": {"universe": n_live, "momentum": n_mom, "passed": len(rows)}, "rules": RULES, "rows": rows}
        print(f"{last}: universe {n_live}, momentum {n_mom}, passed {len(rows)}")
    except Exception as e:
        print("LEADERS FAILED:", e, file=sys.stderr)
        res = dict(prev) if prev.get("rows") else {"rows": [], "rules": RULES}
        res.update(status="stale", reason=str(e)[:300], checked=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    jsave(out_path, res)


if __name__ == "__main__":
    main()
