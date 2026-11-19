"""Daily gold-signal updater. Pulls free public data, scores 10 signals, writes docs/data.json.
Every source is wrapped in try/except: a broken source shows 'no data' instead of crashing the run."""
import io, json, re, sys, datetime as dt
from pathlib import Path
import numpy as np, pandas as pd, requests

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"; DOCS.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (gold-dashboard)"}
S, M, L = 5, 10, 20  # short / medium / long windows (trading days)
DEFAULT = {"wedding_date": "", "import_duty": 0.15, "gst": 0.03, "policy_override": "",
           "buy_score": 0.70, "watch_score": 0.50, "groups_needed": 3}
cfg = dict(DEFAULT)
if (ROOT / "config.json").exists():
    cfg.update(json.loads((ROOT / "config.json").read_text()))
log = []

def safe(name, fn):
    try:
        out = fn()
        log.append(f"OK   {name}")
        return out
    except Exception as e:
        log.append(f"FAIL {name}: {type(e).__name__}: {e}")
        return None

# ---------------- fetchers ----------------
def yf_close(ticker, period="1y"):
    import yfinance as yf
    s = yf.Ticker(ticker).history(period=period, auto_adjust=True)["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].dropna()
    if s.empty: raise ValueError("empty series")
    return s

def fred(series):
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series}, headers=UA, timeout=30)
    r.raise_for_status()
    d = pd.read_csv(io.StringIO(r.text))
    s = pd.Series(pd.to_numeric(d.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(d.iloc[:, 0])).dropna()
    return s[s.index >= pd.Timestamp.today() - pd.Timedelta(days=500)]

def cftc():
    r = requests.get("https://publicreporting.cftc.gov/resource/72hh-3qpy.json", headers=UA, timeout=60, params={
        "$where": "cftc_contract_market_code='088691'",
        "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 150})
    r.raise_for_status()
    d = pd.DataFrame(r.json())
    d["date"] = pd.to_datetime(d["report_date_as_yyyy_mm_dd"]).dt.tz_localize(None).dt.normalize() + pd.Timedelta(days=3)  # Tuesday data, public Friday
    for c in ("m_money_positions_long_all", "m_money_positions_short_all", "open_interest_all"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.set_index("date").sort_index()
    net = d["m_money_positions_long_all"] - d["m_money_positions_short_all"]
    return net.dropna(), d["open_interest_all"].dropna()

def spdr_tonnes():
    url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
    r = requests.get(url, headers=UA, timeout=60); r.raise_for_status()
    lines = r.text.splitlines()
    hi = next(i for i, ln in enumerate(lines[:40]) if "Date" in ln and "Tonnes" in ln)
    d = pd.read_csv(io.StringIO("\n".join(lines[hi:])))
    tc = next(c for c in d.columns if "Tonnes" in c and "Trust" in c)
    s = pd.Series(pd.to_numeric(d[tc].astype(str).str.replace(",", ""), errors="coerce").values,
                  index=pd.to_datetime(d.iloc[:, 0], errors="coerce")).dropna()
    s = s[s.index.notna()].sort_index()
    if s.empty: raise ValueError("no tonnes parsed")
    return s

def sge_close():
    """Shanghai Gold Exchange Au99.99 daily close, CNY per gram (via akshare)."""
    import akshare as ak
    d = ak.spot_hist_sge(symbol="Au99.99")
    dc = next(c for c in d.columns if str(c).lower() in ("date", "日期"))
    cc = next(c for c in d.columns if str(c).lower() in ("close", "收盘价", "收盘"))
    s = pd.Series(pd.to_numeric(d[cc], errors="coerce").values, index=pd.to_datetime(d[dc])).dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if s.empty: raise ValueError("empty SGE series")
    return s[s.index >= pd.Timestamp.today() - pd.Timedelta(days=500)]

def asia_split(h):
    """h: hourly gold bars (Open, Close) indexed by timestamp. Returns (asia_ret, us_ret) per UTC date.
    Asia session = 00:00-07:59 UTC (Tokyo/Shanghai/HK hours, ~05:30-13:30 IST). US session = 12:00-21:59 UTC."""
    h = h.copy()
    idx = pd.to_datetime(h.index)
    idx = idx.tz_convert("UTC") if idx.tz is not None else idx
    h.index = idx.tz_localize(None) if idx.tz is not None else idx
    h["d"] = h.index.normalize(); h["hr"] = h.index.hour
    def sess(lo, hi):
        w = h[(h.hr >= lo) & (h.hr <= hi)]
        g = w.groupby("d")
        return (g["Close"].last() / g["Open"].first() - 1).dropna()
    return sess(0, 7), sess(12, 21)

def gold_sessions():
    import yfinance as yf
    h = yf.Ticker("GC=F").history(period="365d", interval="1h", auto_adjust=True)
    if h.empty: raise ValueError("no hourly data")
    return asia_split(h[["Open", "Close"]])

def yf_snapshot(ticker, field):
    import yfinance as yf
    v = yf.Ticker(ticker).info.get(field)
    if not v: raise ValueError(f"{field} missing")
    return float(v)

def policy_news():
    r = requests.get("https://news.google.com/rss/search", headers=UA, timeout=30,
                     params={"q": "India gold import duty when:14d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    r.raise_for_status()
    titles = re.findall(r"<item>.*?<title>(.*?)</title>", r.text, flags=re.S)
    titles = [re.sub(r"<!\[CDATA\[|\]\]>", "", t).strip() for t in titles][:8]
    up = ("hike", "raise", "increase", "curb", "restrict", "higher duty", "imposes")
    down = ("cut", "reduce", "lower", "slash", "relief", "clarity", "scrap", "waive")
    a = sum(any(k in t.lower() for k in up) for t in titles)
    b = sum(any(k in t.lower() for k in down) for t in titles)
    return titles[:4], (1.0 if b > a else 0.0 if a > b else 0.5)

def asia_gold_news():
    r = requests.get("https://news.google.com/rss/search", headers=UA, timeout=30,
                     params={"q": "\"Asia Gold\" Reuters physical gold India discount when:14d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    r.raise_for_status()
    items = re.findall(r"<item>.*?<title>(.*?)</title>.*?<pubDate>(.*?)</pubDate>", r.text, flags=re.S)
    out = []
    for t, d in items:
        t = re.sub(r"<!\[CDATA\[|\]\]>", "", t).strip()
        if "asia gold" in t.lower(): out.append(f"{t} ({d[:16]})")
    return out[:3]

# ---------------- state: daily snapshots for ETF proxies ----------------
STATE = DOCS / "etf_state.csv"
def load_state():
    if STATE.exists(): return pd.read_csv(STATE, parse_dates=["date"]).set_index("date")
    return pd.DataFrame(columns=["gl_snap", "in_snap"], dtype=float)

# ---------------- build dataset ----------------
def build():
    series = {}
    series["gold"] = safe("gold GC=F", lambda: yf_close("GC=F"))
    series["dxy"] = safe("dollar index DX-Y.NYB", lambda: yf_close("DX-Y.NYB"))
    series["gvz"] = safe("gold vol ^GVZ", lambda: yf_close("^GVZ"))
    series["fx"] = safe("USD/INR INR=X", lambda: yf_close("INR=X")) 
    if series["fx"] is None: series["fx"] = safe("USD/INR FRED", lambda: fred("DEXINUS"))
    series["ry"] = safe("real yield FRED DFII10", lambda: fred("DFII10"))
    series["in_px"] = safe("India gold ETF price GOLDBEES.NS", lambda: yf_close("GOLDBEES.NS"))
    series["cnh"] = safe("USD/CNH CNH=X", lambda: yf_close("CNH=X"))
    if series["cnh"] is None: series["cnh"] = safe("USD/CNY CNY=X", lambda: yf_close("CNY=X"))
    series["sge"] = safe("Shanghai SGE Au99.99 (akshare)", sge_close)
    sess = safe("Asia vs US session gold (hourly)", gold_sessions)
    series["asia_ret"], series["us_ret"] = (sess if sess else (None, None))
    c = safe("CFTC managed money + OI", cftc)
    series["cftc"], series["oi"] = (c if c else (None, None))
    series["gl_etf"] = safe("global ETF SPDR tonnes", spdr_tonnes)
    state = load_state(); today = pd.Timestamp.today().normalize()
    if series["gl_etf"] is None:
        v = safe("global ETF GLD shares (snapshot)", lambda: yf_snapshot("GLD", "sharesOutstanding"))
        if v: state.loc[today, "gl_snap"] = v
        series["gl_etf"] = state["gl_snap"].dropna() if "gl_snap" in state else None
    v = safe("India ETF GOLDBEES assets (snapshot)", lambda: yf_snapshot("GOLDBEES.NS", "totalAssets"))
    if v: state.loc[today, "in_snap"] = v
    series["in_etf"] = state["in_snap"].dropna() if "in_snap" in state else None
    state.index.name = "date"; state.sort_index().to_csv(STATE)
    base = series["gold"]
    if base is None: raise SystemExit("Gold price unavailable; cannot build dashboard\n" + "\n".join(log))
    df = pd.DataFrame(index=base.index)
    for k, s in series.items():
        if s is None or len(s) == 0: df[k] = np.nan; continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if k in ("asia_ret", "us_ret"):
            df[k] = s.reindex(df.index)  # per-day values: never carried forward
        else:
            df[k] = s.reindex(s.index.union(df.index)).ffill(limit=10).reindex(df.index)
    df["asia_cum"] = df["asia_ret"].fillna(0).cumsum().where(df["asia_ret"].notna().cumsum() > 0)
    df["us_cum"] = df["us_ret"].fillna(0).cumsum().where(df["us_ret"].notna().cumsum() > 0)
    # Shanghai premium/(discount) to international gold, US$/oz. SGE gold quote excludes VAT; approximate (different clocks).
    df["sh_prem"] = df["sge"] * 31.1035 / df["cnh"] - df["gold"]
    return df

# ---------------- does the Asia read actually lead Indian gold? ----------------
def lead_lag(df):
    if "in_px" not in df or df["in_px"].notna().sum() < 40: return None
    r = df["in_px"].pct_change(); r[df["in_px"].diff() == 0] = np.nan
    tests = [("Asia-session gold return", df["asia_ret"] if "asia_ret" in df else None),
             ("Shanghai premium change", df["sh_prem"].diff() if "sh_prem" in df else None)]
    out = []
    for label, x in tests:
        if x is None: continue
        d = pd.concat([x, r, r.shift(-1)], axis=1, keys=["x", "same", "nxt"]).dropna(subset=["x"])
        a, b = d.dropna(subset=["same"]), d.dropna(subset=["nxt"])
        if len(a) < 25: continue
        out.append(dict(label=label, n=int(len(a)), same=round(float(a["x"].corr(a["same"])), 2),
                        nxt=round(float(b["x"].corr(b["nxt"])), 2) if len(b) >= 25 else None))
    return out or None

# ---------------- scoring ----------------
def compute(df, news=None, cfg=cfg):
    def col(c): return df[c]
    def v(c, k=0):
        s = df[c]
        if len(s) <= k: return None
        x = s.iloc[-1 - k]
        return None if pd.isna(x) else float(x)
    def pct(c, k):
        a, b = v(c, 0), v(c, k)
        return None if a is None or b is None or b == 0 else a / b - 1
    sig = []
    def add(key, name, group, latest, change, score, why, fmt, chg_fmt, note=""):
        spark = [None if pd.isna(x) else round(float(x), 4) for x in df[key].iloc[-40:]] if key in df else []
        sig.append(dict(key=key, name=name, group=group, latest=latest, change=change, score=score,
                        why=why, fmt=fmt, chg_fmt=chg_fmt, note=note, spark=spark))
    # 1 gold
    g0 = v("gold")
    if g0 is not None and len(df) > L:
        low_r = float(df["gold"].iloc[-S:].min()); low_p = float(df["gold"].iloc[-L:-S].min()); avg = float(df["gold"].iloc[-M:].mean())
        c1, c2 = low_r > low_p, g0 >= avg
        sc = 1 if (c1 and c2) else 0.5 if (c1 or c2) else 0
    else: sc = None
    add("gold", "Gold price", "Price & positioning", g0, pct("gold", M), sc, "Stops making lower lows and holds a recovery", "usd", "pct")
    # 2 dollar
    d0, dch = v("dxy"), pct("dxy", M)
    sc = None
    if d0 is not None and dch is not None and len(df) >= M:
        a = float(df["dxy"].iloc[-M:].mean()); sc = 1 if (dch <= -0.005 and d0 < a) else 0 if dch >= 0.005 else 0.5
    add("dxy", "US dollar", "Dollar & rates", d0, dch, sc, "Sustained weakening", "num2", "pct")
    # 3 real yields
    y0, y1 = v("ry"), v("ry", M)
    ych = None if y0 is None or y1 is None else y0 - y1
    sc = None if ych is None else 1 if ych <= -0.05 else 0 if ych >= 0.05 else 0.5
    add("ry", "US real yields", "Dollar & rates", y0, ych, sc, "Falling rather than rising", "pct2", "pp")
    # 4 CFTC
    c0, c1, c2 = v("cftc"), v("cftc", S), v("cftc", 2 * S)
    if None not in (c0, c1, c2):
        d1, dp = c0 - c1, c1 - c2
        sc = 1 if d1 >= 0 else 1 if (dp < 0 and d1 >= 0.5 * dp) else 0.5 if (dp < 0 and d1 > dp) else 0
        cch = d1
    else: sc, cch = None, None
    add("cftc", "CFTC positioning", "Price & positioning", c0, cch, sc, "Speculative long liquidation slows", "int", "int")
    # 5 COMEX OI (weekly CFTC total OI as proxy)
    o0, och, gch = v("oi"), pct("oi", S), pct("gold", S)
    sc = None
    if och is not None and gch is not None:
        sc = 1 if (och >= -0.01 and gch >= -0.01) else 0 if (och <= -0.03 and gch < 0) else 0.5
    add("oi", "COMEX open interest", "Price & positioning", o0, och, sc, "Price stabilises without renewed aggressive selling", "int", "pct", "Weekly CFTC open interest used as proxy")
    # 6 global ETF
    e0, ech = v("gl_etf"), pct("gl_etf", M)
    sc = None if ech is None else 1 if ech >= 0.002 else 0 if ech <= -0.002 else 0.5
    add("gl_etf", "Global gold ETFs", "Fund flows", e0, ech, sc, "Continued net inflows", "num1", "pct", "SPDR GLD tonnes (or share-count proxy)")
    # 7 India ETF
    i0, ich = v("in_etf"), pct("in_etf", M)
    sc = None if ich is None else 1 if ich >= 0.002 else 0 if ich <= -0.002 else 0.5
    add("in_etf", "Indian gold ETFs", "Fund flows", i0, ich, sc, "Continued holdings growth", "int", "pct", "GOLDBEES assets proxy; needs ~10 days of history")
    # 8 USD/INR
    f0, fch = v("fx"), pct("fx", S)
    sc = None if fch is None else 1 if fch <= 0.003 else 0 if fch >= 0.01 else 0.5
    add("fx", "USD/INR", "India-specific", f0, fch, sc, "No sudden rupee weakness", "num2", "pct")
    # 9 GVZ
    z0, zch = v("gvz"), pct("gvz", S)
    sc = None
    if z0 is not None and zch is not None and len(df) >= L:
        a = float(df["gvz"].iloc[-L:].mean()); sc = 1 if (z0 < a and zch <= 0) else 0 if zch >= 0.10 else 0.5
    add("gvz", "Gold volatility (added)", "Price & positioning", z0, zch, sc, "Calming: below 1-month average, not rising", "num1", "pct")
    # 10 policy
    titles, ps = news if news else ([], None)
    if cfg.get("policy_override"):
        ps = {"supportive": 1.0, "neutral": 0.5, "adverse": 0.0}.get(cfg["policy_override"].lower(), ps)
    add("policy", "Indian policy", "India-specific", None, None, ps, "Clarity on import duty",
        "text", "none", "Auto-read from news headlines (keyword heuristic). Override in config.json")
    sig[-1]["headlines"] = titles
    # 11 Asia-session gold trend (is gold bid during Tokyo/Shanghai/HK hours?)
    a_r = df["asia_ret"].iloc[-M:] if "asia_ret" in df else pd.Series(dtype=float)
    u_r = df["us_ret"].iloc[-M:] if "us_ret" in df else pd.Series(dtype=float)
    sc = None; a_sum = None
    if a_r.notna().sum() >= 6:
        a_sum = float(a_r.sum()); u_sum = float(u_r.sum()) if u_r.notna().sum() >= 6 else 0.0
        sc = 1 if a_sum >= 0.003 else 0 if a_sum <= -0.003 else 0.5
        if sc == 0.5 and a_sum > 0 and u_sum < 0: sc = 1  # Asia buying while US sells
    add("asia_cum", "Asia-session gold (early read)", "Asia (early read)", None if a_sum is None else v("asia_cum"), a_sum, sc,
        "Gold bid during Tokyo/Shanghai/HK hours", "pct1", "pct", "Sum of last 10 Asia-session returns (00-08 UTC)")
    # 12 Shanghai premium
    p0, p1 = v("sh_prem"), v("sh_prem", S)
    sc = None
    if p0 is not None and p1 is not None:
        pch = p0 - p1
        sc = 1 if (p0 >= 0 and pch >= 0) or pch >= 5 else 0 if (p0 < 0 and pch < 0) or pch <= -5 else 0.5
    else: pch = None
    add("sh_prem", "Shanghai premium (added)", "Asia (early read)", p0, pch, sc,
        "Premium rising / back above zero = Chinese physical demand returning", "usd1", "usd",
        "SGE Au99.99 vs international, US$/oz, approximate")
    # 13 yuan
    n0, nch = v("cnh"), pct("cnh", S)
    sc = None if nch is None else 1 if nch <= -0.003 else 0 if nch >= 0.005 else 0.5
    add("cnh", "Yuan (USD/CNH, added)", "Asia (early read)", n0, nch, sc, "Yuan stable or strengthening supports Chinese gold buying", "num4", "pct")
    # groups
    groups = []
    for gname in ["Price & positioning", "Dollar & rates", "Fund flows", "India-specific", "Asia (early read)"]:
        sc_ = [s["score"] for s in sig if s["group"] == gname and s["score"] is not None]
        groups.append(dict(name=gname, score=(sum(sc_) / len(sc_)) if sc_ else None, n=len(sc_),
                           total=sum(1 for s in sig if s["group"] == gname)))
    scored = [s["score"] for s in sig if s["score"] is not None]
    overall = sum(scored) / len(scored) if scored else 0
    confirmed = sum(1 for g in groups if g["score"] is not None and g["score"] >= 0.6)
    gold_sc = sig[0]["score"]
    improving = sum(1 for s in scored if s == 1)
    if len(scored) < 6: verdict = "NOT ENOUGH DATA YET"
    elif overall >= cfg["buy_score"] and confirmed >= cfg["groups_needed"] and (gold_sc or 0) >= 0.5: verdict = "BUY SIGNAL: start staged buying"
    elif gold_sc == 1 and confirmed < 2: verdict = "REBOUND UNCONFIRMED: price alone is not enough"
    elif overall >= cfg["watch_score"]: verdict = "IMPROVING: keep watching"
    else: verdict = "WAIT: outlook has not improved yet"
    parity = None
    if g0 and f0: parity = g0 * f0 / 31.1035 * 10 * (1 + cfg["import_duty"]) * (1 + cfg["gst"])
    days = None
    if cfg.get("wedding_date"):
        try: days = (dt.date.fromisoformat(cfg["wedding_date"]) - dt.date.today()).days
        except ValueError: pass
    return dict(verdict=verdict, overall=overall, improving=improving, scored=len(scored), total=len(sig),
                confirmed=confirmed, needed=cfg["groups_needed"], groups=groups, signals=sig, parity=parity,
                days_to_wedding=days, as_of=str(df.index[-1].date()), lead_lag=lead_lag(df))

if __name__ == "__main__":
    df = build()
    news = safe("policy news (Google News RSS)", policy_news)
    out = compute(df, news)
    out["asia_gold_news"] = safe("Reuters Asia Gold headlines", asia_gold_news) or []
    out["updated_utc"] = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"); out["log"] = log
    (DOCS / "data.json").write_text(json.dumps(out, default=lambda o: None if o is None else str(o)))
    print("\n".join(log)); print("Verdict:", out["verdict"])
