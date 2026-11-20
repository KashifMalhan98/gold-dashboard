"""Gold-market monitoring dashboard data updater. Pulls public data, scores signals,
reports data quality separately from signal strength, and writes docs/data.json.
Every source is wrapped in try/except so partial-source failures are visible."""
import io, json, re, sys, datetime as dt
from pathlib import Path
import numpy as np, pandas as pd, requests

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"; DOCS.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (gold-dashboard)"}
S, M, L = 5, 10, 20  # short / medium / long windows (trading days)
DEFAULT = {"import_duty": 0.15, "gst": 0.03, "policy_override": "",
           "buy_score": 0.70, "watch_score": 0.50, "groups_needed": 3,
           "exclude_from_score": ["gvz", "in_etf"],
           "making_charge_pct": 0.0, "local_premium_pct": 0.0}
cfg = dict(DEFAULT)
if (ROOT / "config.json").exists():
    for _k, _v in json.loads((ROOT / "config.json").read_text()).items():
        if _v != "": cfg[_k] = _v   # an empty value in config.json keeps the built-in default
log = []

def safe(name, fn):
    try:
        out = fn()
        log.append(f"OK   {name}")
        return out
    except Exception as e:
        log.append(f"FAIL {name}: {type(e).__name__}: {e}")
        return None


def append_csv_records(path, records):
    """Append structured observations to CSV, creating a header when needed."""
    if not records:
        return
    normalized = []
    for record in records:
        normalized.append({k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list, tuple)) else v)
                          for k, v in record.items()})
    frame = pd.DataFrame(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, mode="a", index=False, header=not path.exists())

def write_audit_logs(df, out, run_id):
    """Persist market observations, signal states, and a full per-run audit record."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    # Capture the latest available input row per run; historical source series are
    # already used by the backtest and repeating them on every refresh would bloat logs.
    market_rows = []
    if df is not None and not df.empty:
        ix, row = df.iloc[-1].name, df.iloc[-1]
        rec = {"run_id": run_id, "logged_utc": now, "observation_date": str(ix)}
        rec.update({k: (None if pd.isna(v) else v) for k, v in row.items()})
        market_rows.append(rec)
    append_csv_records(DOCS / "market_history.csv", market_rows)

    signal_rows = []
    categories = [("signal", out.get("signals", [])), ("candidate", out.get("cands", [])),
                  ("group", out.get("groups", []))]
    for category, items in categories:
        for item in items or []:
            signal_rows.append({"run_id": run_id, "logged_utc": now, "category": category,
                               "key": item.get("key"), "name": item.get("name"),
                               "group": item.get("group"), "score": item.get("score"),
                               "excluded": item.get("excluded"), "verdict": item.get("verdict"),
                               "details_json": item})
    append_csv_records(DOCS / "signal_history.csv", signal_rows)

    run_record = {"run_id": run_id, "logged_utc": now, "updated_utc": out.get("updated_utc"),
                  "verdict": out.get("verdict"), "short": out.get("short"),
                  "overall": out.get("overall"), "diagnostics": out.get("diagnostics"),
                  "data_quality": out.get("data_quality"), "health": out.get("health"),
                  "signal_count": len(out.get("signals", [])), "candidate_count": len(out.get("cands", [])),
                  "group_count": len(out.get("groups", [])), "signals": out.get("signals", []),
                  "candidates": out.get("cands", []), "groups": out.get("groups", []),
                  "log": out.get("log", []), "backtest": out.get("backtest"),
                  "base_rates": out.get("base_rates"), "confidence": out.get("confidence"),
                  "odds": out.get("odds"), "ibja": out.get("ibja"),
                  "source_data_latest": (df.iloc[-1].to_dict() if df is not None and not df.empty else {})}
    with (DOCS / "run_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(run_record, ensure_ascii=False, default=str) + "\n")

# ---------------- fetchers ----------------
def yf_close(ticker, period="1y"):
    import yfinance as yf
    s = yf.Ticker(ticker).history(period=period, auto_adjust=True)["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].dropna()
    if s.empty: raise ValueError("empty series")
    return s

def fred(series, tries=2, timeout=45):
    last = None
    for _ in range(tries):
        try:
            r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series}, headers=UA, timeout=timeout)
            r.raise_for_status()
            d = pd.read_csv(io.StringIO(r.text))
            s = pd.Series(pd.to_numeric(d.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(d.iloc[:, 0])).dropna()
            return s[s.index >= pd.Timestamp.today() - pd.Timedelta(days=500)]
        except Exception as e:
            last = e
    raise last

def treasury_real_yield():
    """US Treasury daily real yield curve, 10-year. Fallback when FRED is unreachable."""
    out = []
    for yr in (pd.Timestamp.today().year - 1, pd.Timestamp.today().year):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{yr}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={yr}&page&_format=csv")
        r = requests.get(url, headers=UA, timeout=45); r.raise_for_status()
        d = pd.read_csv(io.StringIO(r.text))
        col = next(c for c in d.columns if "10" in str(c))
        out.append(pd.Series(pd.to_numeric(d[col], errors="coerce").values, index=pd.to_datetime(d.iloc[:, 0])))
    s = pd.concat(out).dropna().sort_index()
    if s.empty: raise ValueError("empty Treasury series")
    return s

def treasury_nominal():
    """US Treasury daily nominal yield curve: 2-year and 10-year."""
    out = []
    for yr in (pd.Timestamp.today().year - 1, pd.Timestamp.today().year):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{yr}/all?type=daily_treasury_yield_curve&field_tdr_date_value={yr}&page&_format=csv")
        r = requests.get(url, headers=UA, timeout=45); r.raise_for_status()
        d = pd.read_csv(io.StringIO(r.text)); d.index = pd.to_datetime(d.iloc[:, 0])
        out.append(d)
    d = pd.concat(out).sort_index()
    c2 = next(c for c in d.columns if str(c).strip().startswith("2 Yr"))
    c10 = next(c for c in d.columns if str(c).strip().startswith("10 Yr"))
    return pd.to_numeric(d[c2], errors="coerce").dropna(), pd.to_numeric(d[c10], errors="coerce").dropna()

def real_yield():
    try: return fred("DFII10")
    except Exception as e:
        log.append(f"INFO FRED real yield unreachable ({type(e).__name__}); trying US Treasury site")
        return treasury_real_yield()

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
    hi = next((i for i, ln in enumerate(lines[:60]) if "Date" in ln and "Tonnes" in ln), None)
    if hi is None: raise ValueError("SPDR file layout not recognised: " + r.text[:80].replace("\n", " "))
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

def yf_shares(ticker, days=400):
    """Historical shares outstanding (millions) from Yahoo: creations/redemptions = ETF flows proxy."""
    import yfinance as yf
    sh = yf.Ticker(ticker).get_shares_full(start=(pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d"))
    if sh is None or len(sh) == 0: raise ValueError("no share-count history")
    s = pd.Series(sh.values.astype(float), index=pd.to_datetime(sh.index).tz_localize(None).normalize())
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s / 1e6

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


# ---------------- IBJA official benchmark (ibjarates.com) ----------------
IBJA_FILE = DOCS / "ibja_history.csv"
IBJA_LOG = DOCS / "ibja_scrape.log"
IBJA_DEBUG_HTML = DOCS / "ibja_last_response.html"

def ibja_log(message, append=True):
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with IBJA_LOG.open("a" if append else "w", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")

def ibja_scrape():
    """Fetch and parse IBJA public AM/PM tables; log diagnostics and preserve response HTML."""
    urls = ["https://ibjarates.com/", "https://ibjarates.com/index.aspx"]
    last_error = None
    response = None
    ibja_log("=" * 72)
    ibja_log("IBJA scrape started")
    for url in urls:
        try:
            ibja_log(f"REQUEST url={url}")
            r = requests.get(url, headers=UA, timeout=45)
            ibja_log(f"RESPONSE status={r.status_code} final_url={r.url} content_type={r.headers.get('Content-Type')} bytes={len(r.content)}")
            # Capture even non-2xx pages (e.g. 403 challenge pages) so failures can be inspected.
            try:
                IBJA_DEBUG_HTML.write_text(r.text, encoding="utf-8")
                ibja_log(f"HTML response snapshot saved from {url}: {IBJA_DEBUG_HTML.relative_to(ROOT)}")
                preview = re.sub(r"[\r\n]+", " | ", r.text)[:6000]
                ibja_log(f"HTTP_RESPONSE_PREVIEW={preview}")
            except Exception as snapshot_error:
                ibja_log(f"HTML snapshot/preview failed: {type(snapshot_error).__name__}: {snapshot_error}")
            r.raise_for_status()
            if not r.text.strip():
                raise ValueError("empty response body")
            response = r
            break
        except Exception as e:
            last_error = e
            ibja_log(f"REQUEST_FAIL url={url} error={type(e).__name__}: {e}")
    if response is None:
        raise RuntimeError(f"All IBJA page requests failed: {type(last_error).__name__}: {last_error}")

    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", response.text)
    t = re.sub(r"(?i)</t[dh]>", " ", t)
    t = re.sub(r"(?i)</tr>|<br\s*/?>|</h\d>|</div>|</li>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t).replace("&nbsp;", " ")
    t = re.sub(r"&amp;", "&", t).replace("&#39;", "'").replace("&quot;", '"')
    rows, matched_lines = [], []
    for ln in t.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if not ln:
            continue
        m = re.match(r"^(\d{2}/\d{2}/\d{4}) (\d{4,6}) (\d{4,6}) (\d{4,6}) (\d{4,6}) (\d{4,6})", ln)
        if m:
            matched_lines.append(ln[:300])
            rows.append((dt.datetime.strptime(m.group(1), "%d/%m/%Y").date(), int(m.group(2)), int(m.group(4))))
    preview = re.sub(r"[\r\n]+", " | ", t)[:6000]
    ibja_log(f"TEXT_PREVIEW={preview}")
    ibja_log(f"REGEX_MATCH_COUNT={len(rows)} matched_rows={matched_lines[:20]}")
    blocks = [[]]
    for row in rows:
        if blocks[-1] and row[0] > blocks[-1][-1][0]:
            blocks.append([])
        blocks[-1].append(row)
    if len(blocks) < 2 or not blocks[0]:
        ibja_log(f"PARSE_FAIL table_blocks={len(blocks)} rows_per_block={[len(b) for b in blocks]}")
        raise ValueError("IBJA AM/PM tables not found; inspect docs/ibja_scrape.log and docs/ibja_last_response.html")
    out = []
    for sess, blk in (("AM", blocks[0]), ("PM", blocks[1])):
        for d, p999, p916 in blk:
            if p999 <= 0 or p916 <= 0 or not (0.90 < p916 / p999 < 0.93):
                ibja_log(f"ROW_REJECT session={sess} date={d} p999={p999} p916={p916} reason=purity_ratio")
                continue
            out.append(dict(date=str(d), session=sess, p999=p999, p916=p916))
    if not out:
        ibja_log(f"PARSE_FAIL no valid records; table_blocks={len(blocks)} rows_per_block={[len(b) for b in blocks]}")
        raise ValueError("no valid IBJA rows; inspect docs/ibja_scrape.log and docs/ibja_last_response.html")
    ibja_log(f"PARSE_SUCCESS rows={len(out)} AM={sum(x['session']=='AM' for x in out)} PM={sum(x['session']=='PM' for x in out)} latest={max(x['date'] for x in out)}")
    return out

def ibja_update():
    try:
        new = pd.DataFrame(ibja_scrape()); new["date"] = pd.to_datetime(new["date"])
        old = pd.read_csv(IBJA_FILE, parse_dates=["date"]) if IBJA_FILE.exists() else pd.DataFrame(columns=["date", "session", "p999", "p916"])
        hist = pd.concat([old, new]).drop_duplicates(["date", "session"], keep="last").sort_values(["date", "session"]).reset_index(drop=True)
        hist.to_csv(IBJA_FILE, index=False)
        ibja_log(f"HISTORY_UPDATED added={len(new)} total_rows={len(hist)} path={IBJA_FILE.relative_to(ROOT)}")
        return hist
    except Exception as e:
        ibja_log(f"UPDATE_FAIL {type(e).__name__}: {e}")
        raise

def ibja_calib(df, hist):
    """Premium of the official IBJA 999 rate over import-parity price (world gold x rupee x (1+duty)), averaged over recent prints."""
    if hist is None or len(hist) == 0: return None
    col = "spot" if ("spot" in df and df["spot"].notna().sum() > 30) else "gold"
    obs = []
    for _, r in hist.iterrows():
        d = pd.Timestamp(r["date"]).normalize()
        if d not in df.index: continue
        g, fx = df.at[d, col], df.at[d, "fx"]
        if pd.isna(g) or pd.isna(fx): continue
        obs.append(float(r["p999"]) / 10 / (g * fx / 31.1035 * (1 + cfg["import_duty"])) - 1)
    if not obs: return None
    lr = hist.iloc[-1]
    return dict(premium=float(np.mean(obs[-4:])), n=len(obs), obs=obs,
                latest=dict(date=str(pd.Timestamp(lr["date"]).date()), session=str(lr["session"]), p916_g=float(lr["p916"]) / 10, p999_g=float(lr["p999"]) / 10))

# ---------------- state: daily snapshots for ETF proxies ----------------
STATE = DOCS / "etf_state.csv"
def load_state():
    if STATE.exists(): return pd.read_csv(STATE, parse_dates=["date"]).set_index("date")
    return pd.DataFrame(columns=["gl_snap", "in_snap"], dtype=float)

# ---------------- build dataset ----------------
def build():
    series = {}
    series["gold"] = safe("gold GC=F", lambda: yf_close("GC=F"))
    series["spot"] = safe("spot gold XAUUSD=X", lambda: yf_close("XAUUSD=X"))
    series["dxy"] = safe("dollar index DX-Y.NYB", lambda: yf_close("DX-Y.NYB"))
    series["gvz"] = safe("gold vol ^GVZ", lambda: yf_close("^GVZ"))
    series["fx"] = safe("USD/INR INR=X", lambda: yf_close("INR=X")) 
    if series["fx"] is None: series["fx"] = safe("USD/INR FRED", lambda: fred("DEXINUS"))
    series["ry"] = safe("real yield (FRED, else US Treasury)", real_yield)
    nom = safe("US 2y and 10y nominal yields (Treasury)", treasury_nominal)
    series["y2"], series["nom10"] = (nom if nom else (None, None))
    series["vix"] = safe("VIX ^VIX", lambda: yf_close("^VIX"))
    series["spx"] = safe("S&P 500 ^GSPC", lambda: yf_close("^GSPC"))
    series["oil"] = safe("Oil CL=F", lambda: yf_close("CL=F"))
    series["gdx"] = safe("gold miners GDX", lambda: yf_close("GDX"))
    series["si"] = safe("silver SI=F", lambda: yf_close("SI=F"))
    series["in_px"] = safe("India gold ETF price GOLDBEES.NS", lambda: yf_close("GOLDBEES.NS"))
    series["cnh"] = safe("USD/CNY CNY=X", lambda: yf_close("CNY=X"))
    if series["cnh"] is None: series["cnh"] = safe("USD/CNH CNH=X", lambda: yf_close("CNH=X"))
    series["sge"] = safe("Shanghai SGE Au99.99 (akshare)", sge_close)
    sess = safe("Asia vs US session gold (hourly)", gold_sessions)
    series["asia_ret"], series["us_ret"] = (sess if sess else (None, None))
    c = safe("CFTC managed money + OI", cftc)
    series["cftc"], series["oi"] = (c if c else (None, None))
    state = load_state(); today = pd.Timestamp.today().normalize()
    v = safe("global ETF GLD shares outstanding (daily snapshot)", lambda: yf_snapshot("GLD", "sharesOutstanding"))
    if v: state.loc[today, "gl_snap"] = v / 1e6
    series["gl_etf"] = state["gl_snap"].dropna() if "gl_snap" in state else None
    series["in_etf"] = None  # no free daily source for Indian gold ETF holdings found
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
    df["be10"] = df["nom10"] - df["ry"]   # 10-year breakeven inflation
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

# ---------------- data-quality diagnostics ----------------
def data_quality_report(df, run_log=None):
    """Describe availability and freshness without treating filled values as fresh observations."""
    critical = {"gold", "spot", "fx"}
    report = []
    today = pd.Timestamp.today().normalize()
    for col in df.columns:
        raw = df[col].dropna() if col in df else pd.Series(dtype=float)
        if raw.empty:
            report.append(dict(name=col, status="MISSING", last_date=None, age_days=None,
                              recent_filled=0, recent_total=10, coverage=0.0))
            continue
        last = pd.Timestamp(raw.index[-1]).normalize()
        age = max(0, int((today - last).days))
        recent = int(df[col].tail(10).notna().sum())
        coverage = float(df[col].notna().mean()) if len(df) else 0.0
        # Weekends/holidays are normal. Staleness thresholds are calendar days.
        status = "FRESH" if age <= 4 else "DELAYED" if age <= 10 else "STALE"
        if col in critical and age > 4: status = "STALE"
        report.append(dict(name=col, status=status, last_date=str(last.date()), age_days=age,
                          recent_filled=recent, recent_total=min(10, len(df)), coverage=round(coverage, 3)))
    missing_critical = [x["name"] for x in report if x["name"] in critical and x["status"] == "MISSING"]
    stale_critical = [x["name"] for x in report if x["name"] in critical and x["status"] == "STALE"]
    return dict(rows=report, critical_missing=missing_critical, critical_stale=stale_critical,
               failed_sources=[x for x in (run_log or []) if x.startswith("FAIL")])

def signal_diagnostics(out):
    """Expose signal counts and opposing evidence; this is transparency, not a prediction."""
    ex = set(out.get("excluded_keys", []))
    active = [s for s in out.get("signals", []) if not s.get("excluded") and s.get("score") is not None]
    bullish = sum(s["score"] == 1 for s in active)
    bearish = sum(s["score"] == 0 for s in active)
    neutral = sum(s["score"] == 0.5 for s in active)
    groups = out.get("groups", [])
    group_scores = [g["score"] for g in groups if g.get("score") is not None]
    confirmed = sum(x >= 0.6 for x in group_scores)
    conflict = bool(bullish >= 2 and bearish >= 2)
    return dict(available=len(active), missing=sum(s.get("score") is None and not s.get("excluded") for s in out.get("signals", [])),
               excluded=sum(s.get("excluded") for s in out.get("signals", [])), bullish=bullish,
               bearish=bearish, neutral=neutral, groups_available=len(group_scores),
               groups_confirmed=confirmed, conflicting=conflict,
               note="Signal strength and data reliability are separate. Missing or stale inputs can materially weaken interpretation.")

# ---------------- scoring ----------------
def compute(df, news=None, cfg=cfg, light=False, ibja=None):
    prem_applied = ibja["premium"] if ibja else float(cfg.get("local_premium_pct", 0))
    def col(c): return df[c]
    def v(c, k=0):
        if c not in df: return None
        s = df[c]
        if len(s) <= k: return None
        x = s.iloc[-1 - k]
        return None if pd.isna(x) else float(x)
    def pct(c, k):
        a, b = v(c, 0), v(c, k)
        return None if a is None or b is None or b == 0 else a / b - 1
    sig = []
    def add(key, name, group, latest, change, score, why, fmt, chg_fmt, note=""):
        spark = [None if pd.isna(x) else round(float(x), 4) for x in df[key].iloc[-40:]] if (key in df and not light) else []
        sig.append(dict(key=key, name=name, group=group, latest=latest, change=change, score=score,
                        why=why, fmt=fmt, chg_fmt=chg_fmt, note=note, spark=spark))
    # 1 gold
    g0 = v("gold"); gold_note = ""
    if g0 is not None and len(df) > L:
        low_r = float(df["gold"].iloc[-S:].min()); low_p = float(df["gold"].iloc[-L:-S].min()); avg = float(df["gold"].iloc[-M:].mean())
        c1, c2 = low_r > low_p, g0 >= avg
        gold_note = ("it has stopped making new lows" if c1 else "it is still making new lows")
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
    add("gl_etf", "Global gold ETFs", "Fund flows", e0, ech, sc, "Continued net inflows", "num1", "pct", "GLD shares outstanding (M), built from daily snapshots: scores after ~10 trading days")
    # 7 India ETF
    i0, ich = v("in_etf"), pct("in_etf", M)
    sc = None if ich is None else 1 if ich >= 0.002 else 0 if ich <= -0.002 else 0.5
    add("in_etf", "Indian gold ETFs", "Fund flows", i0, ich, sc, "Continued holdings growth", "int", "pct", "No free daily source found for Indian ETF holdings; not scored")
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
    # IBJA premium (India price versus world price)
    if ibja and ibja["n"] >= 10:
        cur = float(np.mean(ibja["obs"][-2:])); basep = float(np.mean(ibja["obs"][-10:])); dif = cur - basep
        scp = 1 if dif <= -0.005 else 0 if dif >= 0.005 else 0.5
        add("ibja_prem", "India price vs world price (IBJA)", "India-specific", cur, dif, scp, "India's official price not stretched versus the world price", "pct1", "pct", "Official IBJA benchmark versus world gold x rupee x import duty")
    else:
        add("ibja_prem", "India price vs world price (IBJA)", "India-specific", None, None, None, "India's official price not stretched versus the world price", "pct1", "pct", "Building history from the official IBJA rate: scores after about 5 working days")
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
    add("cnh", "Yuan (USD/CNY, added)", "Asia (early read)", n0, nch, sc, "Yuan stable or strengthening supports Chinese gold buying", "num4", "pct")
    ex = set(cfg.get("exclude_from_score", []))
    for _s in sig: _s["excluded"] = _s["key"] in ex
    # groups
    groups = []
    for gname in ["Price & positioning", "Dollar & rates", "Fund flows", "India-specific", "Asia (early read)"]:
        sc_ = [s["score"] for s in sig if s["group"] == gname and s["score"] is not None and s["key"] not in ex]
        groups.append(dict(name=gname, score=(sum(sc_) / len(sc_)) if sc_ else None, n=len(sc_),
                           total=sum(1 for s in sig if s["group"] == gname and s["key"] not in ex)))
    scored = [s["score"] for s in sig if s["score"] is not None and s["key"] not in ex]
    overall = sum(scored) / len(scored) if scored else 0
    confirmed = sum(1 for g in groups if g["score"] is not None and g["score"] >= 0.6)
    gold_sc = sig[0]["score"]
    improving = sum(1 for s in scored if s == 1)
    if len(scored) < 6: verdict = "NOT ENOUGH DATA YET"
    elif overall >= cfg["buy_score"] and confirmed >= cfg["groups_needed"] and (gold_sc or 0) >= 0.5: verdict = "BUY SIGNAL: start staged buying"
    elif gold_sc == 1 and confirmed < 2: verdict = "REBOUND UNCONFIRMED: price alone is not enough"
    elif overall >= cfg["watch_score"]: verdict = "IMPROVING: keep watching"
    else: verdict = "WAIT: outlook has not improved yet"
    cands = []
    def ratio_signal(key, name, num):
        if num not in df: return
        r = (df[num] / df["gold"]).dropna()
        sc = None
        if len(r) > L + S:
            now_, avg_, prev_ = float(r.iloc[-1]), float(r.iloc[-L:].mean()), float(r.iloc[-1 - S])
            sc = 1 if (now_ > avg_ and now_ > prev_) else 0 if (now_ < avg_ and now_ < prev_) else 0.5
        cands.append(dict(key=key, name=name, group="Candidate", score=sc))
    ratio_signal("c_miners", "Candidate: gold miners beating gold", "gdx")
    ratio_signal("c_silver", "Candidate: silver beating gold", "si")
    def chg_candidate(key, name, col, thr, up_green, absolute):
        if col not in df: return
        x = df[col].dropna(); sc = None
        if len(x) > M + 1:
            ch = float(x.iloc[-1] - x.iloc[-1 - M]) if absolute else float(x.iloc[-1] / x.iloc[-1 - M] - 1)
            ch = ch if up_green else -ch
            sc = 1 if ch >= thr else 0 if ch <= -thr else 0.5
        cands.append(dict(key=key, name=name, group="Candidate", score=sc))
    chg_candidate("c_y2", "Candidate: US 2-year yield falling", "y2", 0.10, False, True)
    chg_candidate("c_be", "Candidate: breakeven inflation rising", "be10", 0.05, True, True)
    chg_candidate("c_oil", "Candidate: oil falling", "oil", 0.03, False, False)
    chg_candidate("c_vix", "Candidate: VIX (stress) falling", "vix", 0.10, False, False)
    chg_candidate("c_spx", "Candidate: S&P 500 rising", "spx", 0.02, True, False)
    drivers = []
    for col, name, unit, absolute in (("y2", "US 2-year yield (rate-hike pressure)", "%", True), ("be10", "10-yr breakeven inflation", "%", True),
                                      ("oil", "Oil (WTI, US$)", "$", False), ("spx", "S&P 500", "", False), ("vix", "VIX (equity stress)", "", False)):
        if col in df and df[col].notna().sum() > M + 1:
            x = df[col].dropna(); now_ = float(x.iloc[-1]); old_ = float(x.iloc[-1 - M])
            drivers.append(dict(name=name, unit=unit, latest=now_, change=(now_ - old_) if absolute else (now_ / old_ - 1), absolute=absolute))
    ctx = {}
    gs = df["gold"].dropna()
    if len(gs) >= 60:
        w = gs.iloc[-250:]; hi, lo, now = float(w.max()), float(w.min()), float(gs.iloc[-1])
        ctx.update(gold=now, from_high=now / hi - 1, range_pos=(now - lo) / (hi - lo) if hi > lo else None,
                   vs50=now / float(gs.iloc[-50:].mean()) - 1)
        if len(gs) >= 200: ctx["vs200"] = now / float(gs.iloc[-200:].mean()) - 1
        if "fx" in df:
            pb = "spot" if ("spot" in df and df["spot"].notna().sum() > 60) else "gold"
            inr = (df[pb] * df["fx"] / 31.1035 * (1 + cfg["import_duty"]) * 0.916 * (1 + prem_applied)).dropna()
            if len(inr) >= 60:
                z = inr.iloc[-60:]
                ctx.update(inr=float(inr.iloc[-1]), inr_chg10=float(inr.iloc[-1] / inr.iloc[-1 - M] - 1),
                           inr_pos=float((inr.iloc[-1] - z.min()) / (z.max() - z.min())) if z.max() > z.min() else None)
    short = "SUPPORTIVE" if verdict.startswith("BUY") else "MIXED" if verdict.startswith(("IMPROVING", "REBOUND")) else "UNCONFIRMED"
    gch = pct("gold", M)
    trend = "" if gch is None else f"Gold is {'down' if gch < 0 else 'up'} {abs(gch) * 100:.1f}% over the last 2 weeks" + (f"; {gold_note}." if gold_note else ".")
    msg = {"SUPPORTIVE": "Multiple scored indicators currently support stabilisation, but the model is experimental.",
           "MIXED": "Some indicators support stabilisation; others remain inconclusive or disagree.",
           "UNCONFIRMED": "The available indicators do not yet confirm stabilisation."}[short]
    if verdict.startswith("REBOUND"): msg = "Price bounced, but other indicators do not confirm it. Do not chase it."
    parity = None; rates = None
    g_sp = v("spot") or g0
    if g_sp and f0:
        base24 = g_sp * f0 / 31.1035                       # Rs per gram of pure gold before duty
        p24_ex = base24 * (1 + cfg["import_duty"])         # with import duty, before GST (how IBJA-style rates are quoted)
        p22_ex = p24_ex * 0.916 * (1 + prem_applied)
        rates = dict(gold_usd=g_sp, usdinr=f0, source="spot gold" if v("spot") else "gold futures", base24=base24, p24_ex=p24_ex,
                     p22_ex=p22_ex, p22_gst=p22_ex * (1 + cfg["gst"]), p22_all=p22_ex * (1 + cfg["gst"]) * (1 + float(cfg["making_charge_pct"])),
                     duty=cfg["import_duty"], gst=cfg["gst"], making=float(cfg["making_charge_pct"]), local_premium=prem_applied,
                     calibrated=bool(ibja), ibja=(ibja["latest"] if ibja else None))
        parity = p24_ex * (1 + cfg["gst"]) * 10
    return dict(short=short, msg=msg, trend=trend, verdict=verdict, overall=overall, improving=improving, scored=len(scored), total=len([s for s in sig if s['key'] not in ex]),
                confirmed=confirmed, needed=cfg["groups_needed"], groups=groups, signals=sig, parity=parity, rates=rates, n_all=len(sig), n_excluded=sum(1 for x in sig if x['key'] in ex), n_nodata=sum(1 for x in sig if x['key'] not in ex and x['score'] is None),
                as_of=str(df.index[-1].date()), lead_lag=(None if light else lead_lag(df)), cands=cands, context=ctx, drivers=drivers, excluded_keys=sorted(ex))

# ---------------- backtest: did each signal actually help? ----------------
def backtest(df, horizon=10, max_days=200, min_n=20):
    n = len(df); gold = df["gold"].values
    recs, names = [], {}
    for i in range(max(40, n - max_days), n - 1):
        if i + horizon >= n or np.isnan(gold[i]) or np.isnan(gold[i + horizon]): continue
        o = compute(df.iloc[:i + 1], ([], None), cfg, light=True)
        for sg in o["signals"] + o["cands"]: names[sg["key"]] = (sg["name"], sg["group"])
        sc = {sg["key"]: sg["score"] for sg in o["signals"] + o["cands"]}
        recs.append((sc, float(gold[i + horizon] / gold[i] - 1), o["short"]))
    if len(recs) < 40: return None
    allf = np.array([f for _, f, _ in recs])
    def stats(x): 
        x = np.array(x); return (len(x), float(x.mean()) if len(x) else None, float((x > 0).mean()) if len(x) else None)
    rows = []
    for key, (nm, grp) in names.items():
        g = [f for sc, f, _ in recs if sc.get(key) == 1]; r = [f for sc, f, _ in recs if sc.get(key) == 0]
        ng, mg, hg = stats(g); nr, mr, hr = stats(r)
        if ng < min_n or nr < min_n: verdict = "too few observations"
        else:
            edge, hedge = mg - mr, hg - hr
            verdict = "helps" if (edge >= 0.01 and hedge >= 0.15) else "backwards" if (edge <= -0.01 and hedge <= -0.15) else "no clear edge"
        rows.append(dict(key=key, name=nm, candidate=(grp == "Candidate"), n_green=ng, n_red=nr, green_avg=mg, red_avg=mr,
                         green_hit=hg, red_hit=hr, edge=None if (mg is None or mr is None) else mg - mr, verdict=verdict))
    buckets = []
    for b in ("BUY", "WATCH", "WAIT"):
        nb, mb, hb = stats([f for _, f, sh in recs if sh == b]); buckets.append(dict(name=b, n=nb, avg=mb, hit=hb))
    # Signal-score correlations reveal checks that may be telling the same story.
    score_frame = pd.DataFrame([sc for sc, _, _ in recs])
    redundant_pairs = []
    if not score_frame.empty:
        corr = score_frame.corr(min_periods=30)
        keys = list(corr.columns)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                value = corr.loc[a, b]
                if pd.notna(value) and abs(float(value)) >= 0.65:
                    na = int(score_frame[[a, b]].dropna().shape[0])
                    redundant_pairs.append(dict(a=a, b=b, correlation=round(float(value), 2), n=na))
        redundant_pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
    return dict(days=len(recs), horizon=horizon, base_avg=float(allf.mean()), base_hit=float((allf > 0).mean()),
                rows=rows, buckets=buckets, redundant_pairs=redundant_pairs[:12])

# ---------------- 25-year base rates ----------------
def base_rates(g):
    g = g.dropna(); g = g[g > 0]
    if len(g) < 1500: return None
    hi = g.rolling(252, min_periods=200).max(); dd = g / hi - 1
    ma200 = g.rolling(200).mean(); gap = g / ma200 - 1
    d = g.diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean(); rsi = 100 - 100 / (1 + up / dn)
    hz = {"1 month": 21, "3 months": 63, "6 months": 126, "12 months": 252}
    fwd = {k: g.shift(-h) / g - 1 for k, h in hz.items()}
    adverse = (g[::-1].rolling(63, min_periods=20).min()[::-1].shift(-1) / g - 1)
    conds = [("All days (baseline)", pd.Series(True, index=g.index)),
             ("20%+ below its 1-year high", dd <= -0.20),
             ("10%+ below its 200-day average", gap <= -0.10),
             ("Oversold (RSI below 30)", rsi <= 30),
             ("20%+ below high AND 10%+ below 200-day", (dd <= -0.20) & (gap <= -0.10))]
    def episodes(m):
        pos = np.flatnonzero(m.values)
        return 0 if len(pos) == 0 else int(1 + (np.diff(pos) > 60).sum())
    rows = []
    for name, mask in conds:
        m = mask & dd.notna()
        row = dict(name=name, days=int(m.sum()), episodes=episodes(m))
        for k in hz:
            x = fwd[k][m].dropna()
            row[k] = dict(n=int(len(x)), median=float(x.median()) if len(x) else None, pos=float((x > 0).mean()) if len(x) else None,
                          p10=float(x.quantile(0.10)) if len(x) else None, p90=float(x.quantile(0.90)) if len(x) else None)
        a = adverse[m].dropna()
        row["further_drop_median"] = float(a.median()) if len(a) else None
        row["further_drop_worst10"] = float(a.quantile(0.10)) if len(a) else None
        rows.append(row)
    return dict(since=str(g.index[0].date()), now=dict(from_high=float(dd.iloc[-1]), vs200=float(gap.iloc[-1]), rsi=float(rsi.iloc[-1])), rows=rows)

# Individual purchase schedules are intentionally omitted from this market-monitoring project.

def odds_from(br):
    now = br["now"]; pick = "All days (baseline)"
    if now["from_high"] <= -0.20 and now["vs200"] <= -0.10: pick = "20%+ below high AND 10%+ below 200-day"
    elif now["from_high"] <= -0.20: pick = "20%+ below its 1-year high"
    elif now["vs200"] <= -0.10: pick = "10%+ below its 200-day average"
    elif now["rsi"] <= 30: pick = "Oversold (RSI below 30)"
    r = next(x for x in br["rows"] if x["name"] == pick)
    return dict(label=pick, p3=r["3 months"]["pos"], p12=r["12 months"]["pos"], med3=r["3 months"]["median"], med12=r["12 months"]["median"],
                days=r["days"], episodes=r["episodes"])

def confidence_from(bt):
    if not bt: return dict(level="LOW", helps=0, tested=0)
    scored = [r for r in bt["rows"] if not r["candidate"] and r["verdict"] != "too few observations"]
    helps = sum(1 for r in scored if r["verdict"] == "helps")
    level = "LOW" if helps == 0 else "MEDIUM" if helps <= 2 else "HIGHER"
    return dict(level=level, helps=helps, tested=len(scored))

if __name__ == "__main__":
    df = build()
    news = safe("policy news (Google News RSS)", policy_news)
    ibja_hist = safe("IBJA official rates (ibjarates.com)", ibja_update)
    ibja = ibja_calib(df, ibja_hist) if ibja_hist is not None else None
    out = compute(df, news, cfg, False, ibja)
    out["asia_gold_news"] = safe("Reuters Asia Gold headlines", asia_gold_news) or []
    out["data_quality"] = safe("data quality report", lambda: data_quality_report(df, log)) or {"rows": [], "critical_missing": [], "critical_stale": [], "failed_sources": log.copy()}
    out["diagnostics"] = signal_diagnostics(out)
    out["health"] = [f"{r['name']}: {r['status']}, last observation {r['last_date'] or 'NONE'}, {r['recent_filled']}/{r['recent_total']} recent rows, {r['coverage']*100:.0f}% coverage" for r in out["data_quality"].get("rows", [])]
    long_g = safe("gold long history (25y) GC=F", lambda: yf_close("GC=F", period="max"))
    out["base_rates"] = safe("25-year base rates", lambda: base_rates(long_g)) if long_g is not None else None
    out["backtest"] = safe("backtest of signals", lambda: backtest(df))
    out["odds"] = safe("odds from history", lambda: odds_from(out["base_rates"])) if out.get("base_rates") else None
    out["confidence"] = confidence_from(out.get("backtest"))
    out["updated_utc"] = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"); out["log"] = log
    (DOCS / "data.json").write_text(json.dumps(out, default=lambda o: None if o is None else str(o), indent=2), encoding="utf-8")
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        write_audit_logs(df, out, run_id)
        log.append("OK   audit logging: market_history.csv, signal_history.csv, run_history.jsonl")
    except Exception as e:
        log.append(f"FAIL audit logging: {type(e).__name__}: {e}")
    print("\n".join(log)); print("Verdict:", out["verdict"])
