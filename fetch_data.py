"""Gold-market monitoring dashboard data updater. Pulls public data, scores signals,
reports data quality separately from signal strength, and writes docs/data.json.
Every source is wrapped in try/except so partial-source failures are visible."""
import io, json, re, sys, datetime as dt
from html.parser import HTMLParser
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

class _IBJATableParser(HTMLParser):
    """Small stdlib parser that keeps table/row/cell boundaries from IBJA HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = {"rows": [], "text": []}
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
    def handle_data(self, data):
        text = data.strip()
        if not text: return
        if self.cell is not None: self.cell.append(text)
        if self.table is not None: self.table["text"].append(text)
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join(self.cell).strip()); self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            if self.row: self.table["rows"].append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table["rows"]: self.tables.append(self.table)
            self.table = None


def _ibja_date(value):
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
        try: return dt.datetime.strptime(value.strip(), fmt).date()
        except ValueError: pass
    return None


def _ibja_numbers(cells):
    values = []
    for cell in cells:
        m = re.search(r"(?<!\w)(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d+)?(?!\w)", cell)
        if m:
            try: values.append(float(m.group(1).replace(",", "")))
            except ValueError: pass
    return values


def _ibja_parse_tables(html):
    parser = _IBJATableParser(); parser.feed(html)
    ibja_log(f"HTML_TABLE_COUNT={len(parser.tables)}")
    parsed_tables = []
    for ti, table in enumerate(parser.tables):
        rows = table["rows"]
        header_text = " ".join(" ".join(r) for r in rows[:3]).lower()
        session = "AM" if re.search(r"\b(am|morning)\b", header_text) else "PM" if re.search(r"\b(pm|evening)\b", header_text) else None
        ix999 = next((i for r in rows[:3] for i, c in enumerate(r) if re.search(r"999", c)), None)
        ix916 = next((i for r in rows[:3] for i, c in enumerate(r) if re.search(r"916|91\.6", c)), None)
        extracted = []
        for ri, row in enumerate(rows):
            joined = " ".join(row)
            dm = re.search(r"\b(\d{2}/\d{2}/\d{4}|\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2} [A-Za-z]{3,9} \d{4})\b", joined)
            date = _ibja_date(dm.group(1)) if dm else None
            if not date: continue
            nums = _ibja_numbers(row)
            if ix999 is not None and ix916 is not None and ix999 < len(row) and ix916 < len(row):
                try:
                    p999 = _ibja_numbers([row[ix999]])[0]
                    p916 = _ibja_numbers([row[ix916]])[0]
                except (IndexError, TypeError):
                    p999 = p916 = None
            else:
                # Conservative legacy-layout fallback: table row has six numeric values
                # and the previous scraper's columns 2 and 4 represented 999 and 916.
                p999 = nums[1] if len(nums := _ibja_numbers(row)) >= 4 else None
                p916 = nums[3] if len(nums) >= 4 else None
            if p999 is not None and p916 is not None:
                extracted.append((date, p999, p916))
        parsed_tables.append({"session": session, "rows": extracted, "header": header_text[:240]})
        ibja_log(f"TABLE[{ti}] session={session} rows={len(extracted)} header={header_text[:240]!r}")
    return parsed_tables



def ibja_scrape():
    """Fetch IBJA page, extract table rows conservatively, and log diagnostics."""
    urls = ["https://ibjarates.com/", "https://ibjarates.com/index.aspx"]
    last_error = None; response = None
    ibja_log("=" * 72); ibja_log("IBJA scrape started")
    for url in urls:
        try:
            ibja_log(f"REQUEST url={url}")
            r = requests.get(url, headers=UA, timeout=45)
            ibja_log(f"RESPONSE status={r.status_code} final_url={r.url} content_type={r.headers.get('Content-Type')} bytes={len(r.content)}")
            try:
                IBJA_DEBUG_HTML.write_text(r.text, encoding="utf-8")
                ibja_log(f"HTML response snapshot saved: {IBJA_DEBUG_HTML.relative_to(ROOT)}")
                preview = re.sub(r"[\r\n]+", " | ", r.text)[:6000]
                ibja_log(f"HTTP_RESPONSE_PREVIEW={preview}")
            except Exception as e: ibja_log(f"HTML snapshot failed: {type(e).__name__}: {e}")
            r.raise_for_status()
            if not r.text.strip(): raise ValueError("empty response body")
            response = r; break
        except Exception as e:
            last_error = e; ibja_log(f"REQUEST_FAIL url={url} error={type(e).__name__}: {e}")
    if response is None:
        raise RuntimeError(f"All IBJA page requests failed: {type(last_error).__name__}: {last_error}")

    tables = _ibja_parse_tables(response.text)
    valid_tables = [t for t in tables if t["rows"]]
    # Explicit table labels win; if labels are absent, AM/PM are assigned by
    # table order only when exactly two distinct rate tables were found.
    session_tables = {t["session"]: t for t in valid_tables if t["session"]}
    unlabeled = [t for t in valid_tables if not t["session"]]
    if not session_tables and len(valid_tables) == 2:
        session_tables = {"AM": valid_tables[0], "PM": valid_tables[1]}
    elif unlabeled and len(unlabeled) == 2:
        session_tables.setdefault("AM", unlabeled[0]); session_tables.setdefault("PM", unlabeled[1])
    out = []
    for sess in ("AM", "PM"):
        table = session_tables.get(sess)
        if not table: continue
        for d, p999, p916 in table["rows"]:
            if p999 <= 0 or p916 <= 0 or not (0.90 < p916 / p999 < 0.93):
                ibja_log(f"ROW_REJECT session={sess} date={d} p999={p999} p916={p916} reason=purity_ratio_or_invalid")
                continue
            out.append(dict(date=str(d), session=sess, p999=int(p999), p916=int(p916)))
    if not out:
        ibja_log(f"PARSE_FAIL tables={len(tables)} valid_tables={len(valid_tables)} rows_per_table={[len(t['rows']) for t in tables]}")
        raise ValueError("IBJA rates tables not parsed or rates unpublished; inspect scrape log and saved HTML")
    out = list({(r["date"], r["session"]): r for r in out}.values())
    ibja_log(f"PARSE_SUCCESS rows={len(out)} AM={sum(x['session']=='AM' for x in out)} PM={sum(x['session']=='PM' for x in out)} latest={max(x['date'] for x in out)}")
    return out

def ibja_update():
    try:
        new = pd.DataFrame(ibja_scrape()); new["date"] = pd.to_datetime(new["date"])
        old = pd.read_csv(IBJA_FILE, parse_dates=["date"]) if IBJA_FILE.exists() else pd.DataFrame(columns=["date", "session", "p999", "p916"])
        hist = pd.concat([old, new]).drop_duplicates(["date", "session"], keep="last").sort_values(["date", "session"]).reset_index
