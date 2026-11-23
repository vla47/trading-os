// Trading OS live prices — Cloudflare Worker.
// GET /quotes -> current price for every ticker on the page, from Yahoo Finance, with the SAME rules as scripts/quotes.py:
//   1D = price / prevClose - 1 | 1W/1M/3M/6M = price / close 5/21/63/126 sessions back | YTD = vs last close of the previous year
//   21ema dot, VIX dot, |1D| limit, stale > 30 min while open, QQQ pre/after-hours, NQ/ES move since the 16:00 ET cash close.
// Daily closes come from live_base.json (written by quotes.py on each site build); only the live price is fetched here.
// Responses are cached 60 s at the edge, so Yahoo sees at most one round of requests per minute however often the page asks.

const SITE = "https://vla47.github.io/trading-os/";
const YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/";
const CACHE_SECONDS = 60;
const UA = { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36" };
const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS" };

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    if (url.pathname !== "/quotes") return new Response("Trading OS live prices: GET /quotes", { status: 404, headers: CORS });
    const cache = caches.default, key = new Request(url.origin + "/quotes");
    const hit = await cache.match(key);
    if (hit) return hit;
    let body, status = 200;
    try { body = await buildQuotes(); }
    catch (e) { body = { error: String(e && e.message || e) }; status = 502; }
    const res = new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json",
      "Cache-Control": `public, max-age=${status === 200 ? CACHE_SECONDS : 15}` } });
    if (status === 200) ctx.waitUntil(cache.put(key, res.clone()));
    return res;
  },
};

// ---------------------------------------------------------------------------------------------------------------
export async function buildQuotes(now = new Date()) {
  const base = await getJson(SITE + "live_base.json", 600);
  if (!base || !base.tickers) throw new Error("live_base.json not available");
  const nowS = now.getTime() / 1000, cash = {}, quotes = {}, failed = [];
  const keys = Object.keys(base.tickers);
  const one = async (k) => {
    try { quotes[k] = await liveQuote(k, base.tickers[k], base, cash, nowS); }
    catch (e) { failed.push(`${k}: ${e.message || e}`); }
  };
  // the US cash session (from QQQ) decides NQ/ES pre-market / after-hours, so it goes first
  if (base.tickers[base.cash]) await one(base.cash);
  await Promise.all(keys.filter((k) => k !== base.cash).map(one));
  if (!Object.keys(quotes).length) throw new Error("no quotes: " + failed.join("; "));
  return { version: 1, updated: iso(nowS, "seconds"), source: "Yahoo Finance", base: base.updated, quotes, failed };
}

async function liveQuote(key, t, base, cash, nowS) {
  const r = await chart(t.symbol, "range=1d&interval=1m&includePrePost=true");
  const m = r.meta || {}, dec = t.dec;
  const price = num(m.regularMarketPrice), asOf = m.regularMarketTime || null;
  const tz = m.exchangeTimezoneName || "UTC", reg = (m.currentTradingPeriod || {}).regular || {};
  // ---- checks (same as quotes.py) ----
  let prev = num(m.chartPreviousClose);
  if (!(price > 0 && prev > 0)) throw new Error(`bad price ${price} / prev ${prev}`);
  const session = asOf ? ymd(asOf, tz) : t.closes[t.closes.length - 1][0];
  const done = t.closes.filter(([d]) => d < session);          // completed sessions only
  if (!done.length) throw new Error("no daily history");
  const warn = [];
  const lastDone = done[done.length - 1][1];
  if (Math.abs(lastDone / prev - 1) > 0.01) {
    // Yahoo's previous-close field sometimes lags a day (seen on ^VIX); outside futures the last daily close is the truth
    if (!t.symbol.endsWith("=F")) { warn.push(`Yahoo prev close ${prev.toFixed(dec)} ≠ last daily close ${lastDone.toFixed(dec)}; using the daily close`); prev = lastDone; }
    else warn.push(`previous bar ${lastDone.toFixed(dec)} ≠ prev close ${prev.toFixed(dec)} (contract roll / data gap)`);
  }
  const chg = pct(price, prev);
  if (Math.abs(chg) > t.max) throw new Error(`1D ${chg}% beyond ±${t.max}%`);
  const start = reg.start, end = reg.end;
  const open = !!(start && end && start <= nowS && nowS < end);
  const age = asOf ? nowS - asOf : null;
  const stale = !!(open && age != null && age > base.maxAgeOpen);
  if (stale) warn.push(`quote ${Math.floor(age / 60)} min old while the market is open`);
  // ---- one series: completed closes + live price ----
  const s = [...done.map((x) => x[1]), price], dates = [...done.map((x) => x[0]), session];
  const perf = { "1D": chg };
  for (const [p, n] of base.periods) perf[p] = s.length > n ? pct(price, s[s.length - 1 - n]) : null;
  const yr = session.slice(0, 4);
  let py = -1; for (let i = dates.length - 1; i >= 0; i--) if (dates[i].slice(0, 4) < yr) { py = i; break; }
  perf.YTD = py >= 0 ? pct(price, s[py]) : null;
  const rec = { price: rnd(price, dec), prevClose: rnd(prev, dec), chg, perf, asOf: asOf ? iso(asOf, "seconds") : null,
    open, stale, session, warn: warn.length ? warn : null, ext: null, eth: null };
  if (t.dot === "ema") rec.dot = emaDot(s);
  else if (t.dot === "vix") rec.dot = price > 25 ? "red" : price >= 15 ? "yellow" : "green";
  if (key === base.cash) Object.assign(cash, { open, close: asOf, start });
  if (base.ext.includes(key)) rec.ext = extQuote(r, price, reg, dec);
  if (base.near23h.includes(key) && cash.close && !cash.open) {
    try { rec.eth = await sinceCashClose(t.symbol, dec, cash, nowS); } catch (e) { /* keep the settlement view */ }
  }
  return rec;
}

// latest pre-market / after-hours trade vs the regular-session price (null inside regular hours)
function extQuote(r, regPrice, reg, dec) {
  const ts = r.timestamp || [], c = ((r.indicators || {}).quote || [{}])[0].close || [];
  let i = ts.length - 1; while (i >= 0 && c[i] == null) i--;
  if (i < 0 || !reg.start || !reg.end) return null;
  const t = ts[i], last = c[i];
  if (reg.start <= t && t < reg.end) return null;
  return { session: t < reg.start ? "pre" : "post", price: rnd(last, dec), chg: pct(last, regPrice), time: iso(t, "minutes") };
}

// futures move since the last US cash close (16:00 ET); the 16:59 ET bar (CME halt, settlement stamp) is dropped
async function sinceCashClose(sym, dec, cash, nowS) {
  const r = await chart(sym, "range=5d&interval=1m");
  const ts = r.timestamp || [], c = ((r.indicators || {}).quote || [{}])[0].close || [];
  let ref = null, last = null, lastT = null;
  for (let i = 0; i < ts.length; i++) {
    if (c[i] == null) continue;
    const [h, mi] = etHM(ts[i]);
    if (h === 16 && mi === 59) continue;
    if (ts[i] < cash.close - 59) ref = c[i];
    else if (ts[i] >= cash.close) { last = c[i]; lastT = ts[i]; }
  }
  if (ref == null || last == null) return null;
  return { session: cash.start && nowS < cash.start ? "pre" : "post", price: rnd(last, dec), chg: pct(last, ref),
    ref: rnd(ref, dec), refTime: iso(cash.close, "minutes"), time: iso(lastT, "minutes") };
}

function emaDot(series) {
  const c = series.slice(-130), k = 2 / 22, e = [];
  c.forEach((v, i) => e.push(i ? v * k + e[i - 1] * (1 - k) : v));
  const n = c.length;
  if (c[n - 1] < e[n - 1]) return "red";
  let below = false; for (let i = n - 6; i < n - 1; i++) if (i >= 0 && c[i] < e[i]) below = true;
  const rising = e[n - 1] > e[n - 6] * 1.001;
  return below || !rising ? "yellow" : "green";
}

// ---- helpers ----
async function chart(sym, q) {
  const res = await fetch(YAHOO + encodeURIComponent(sym) + "?" + q, { headers: UA });
  if (!res.ok) throw new Error(`Yahoo HTTP ${res.status}`);
  const j = await res.json(), r = j && j.chart && j.chart.result && j.chart.result[0];
  if (!r) throw new Error("Yahoo: no data" + (j?.chart?.error?.description ? " (" + j.chart.error.description + ")" : ""));
  return r;
}
async function getJson(u, ttl) {
  const res = await fetch(u, { cf: { cacheTtl: ttl, cacheEverything: true } });
  return res.ok ? res.json() : null;
}
const num = (v) => (typeof v === "number" && isFinite(v) ? v : NaN);
const pct = (a, b) => (b ? Math.round((a / b - 1) * 10000) / 100 : null);
const rnd = (v, d) => Math.round(v * 10 ** d) / 10 ** d;
const iso = (s, unit) => { const x = new Date(s * 1000).toISOString(); return (unit === "minutes" ? x.slice(0, 16) : x.slice(0, 19)) + "+00:00"; };
function ymd(s, tz) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(new Date(s * 1000)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}
function etHM(s) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .formatToParts(new Date(s * 1000)).map((x) => [x.type, x.value]));
  return [Number(p.hour), Number(p.minute)];
}
