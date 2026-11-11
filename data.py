"""
Shared data layer for the IHSG dashboard (used by app.py and streamlit_app.py).

* The index (^JKSE) lives in table `prices` (same as ihsg_db.py).
* Every IDX stock lives in `stock_prices`, loaded lazily the first time you open it
  and then updated incrementally (at most once per hour per symbol).
* The list of all IDX stocks lives in `stocks`. It is filled from Yahoo's screener
  (exchange = JKT, i.e. all stocks listed on the Indonesia Stock Exchange), from your
  own uploaded IDX list, or from the bundled starter list `idx_stocks.csv`.
"""
import sqlite3
import time
import zlib
from pathlib import Path

import pandas as pd

import ihsg_db as core

HERE = Path(__file__).resolve().parent
INDEX_SYM = core.TICKER  # "^JKSE"
EPOCH = pd.Timestamp("1970-01-01")
FULL_UNIVERSE_MIN = 300  # fewer than this => the list is only a partial/starter list

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_prices (
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
    adj_close REAL, volume INTEGER, PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS stocks (symbol TEXT PRIMARY KEY, name TEXT);
"""


def connect(db):
    conn = core.connect(db)
    conn.executescript(EXTRA_SCHEMA)
    return conn


# ------------------------------------------------------------------ symbols
def norm(sym):
    """'bbca' / 'BBCA' / 'BBCA.JK' -> 'BBCA.JK';  'IHSG' / '^JKSE' -> '^JKSE'."""
    s = (sym or "").strip().upper()
    if s in ("", "IHSG", "JKSE", "^JKSE", "COMPOSITE"):
        return INDEX_SYM
    if "." not in s and not s.startswith("^"):
        s += ".JK"
    return s


def label(sym):
    return "IHSG" if sym == INDEX_SYM else sym.rsplit(".", 1)[0]


# ------------------------------------------------------------------- prices
def _last_date(conn, sym):
    if sym == INDEX_SYM:
        return conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    return conn.execute("SELECT MAX(date) FROM stock_prices WHERE symbol=?", (sym,)).fetchone()[0]


def _fake(sym, start):
    seed = zlib.crc32(sym.encode())
    base = 4500 if sym == INDEX_SYM else 300 + seed % 6000
    return core.fake_data(start if start else "2015-01-01", seed=seed, base=base)


def _upsert(conn, sym, df):
    if sym == INDEX_SYM:
        return core.upsert(conn, df)
    rows = [
        (sym, d.strftime("%Y-%m-%d"), r.Open, r.High, r.Low, r.Close, r.AdjClose, int(r.Volume))
        for d, r in df.iterrows()
    ]
    conn.executemany("INSERT OR REPLACE INTO stock_prices VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def update_symbol(db, sym, demo=False, force=False, max_age=3600):
    """Download new bars for `sym` from yfinance. Returns rows fetched (0 = skipped).
    Raises if there is nothing at all for the symbol."""
    sym = norm(sym)
    conn = connect(db)
    try:
        key = f"fetched:{sym}"
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        now = time.time()
        if row and not force and now - float(row[0]) < max_age:
            return 0
        last = _last_date(conn, sym)
        if last:
            start = (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            start = "1997-07-01" if sym == INDEX_SYM else "1990-01-01"
        df = _fake(sym, start if last else None) if demo else core.fetch_yfinance(start, sym)
        if df is None or df.empty:
            if last:
                return 0
            raise RuntimeError(f"No data returned for {sym} (wrong code, or yfinance unavailable)")
        n = _upsert(conn, sym, df)
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(now)))
        conn.commit()
        return n
    finally:
        conn.close()


def load(db, sym):
    sym = norm(sym)
    conn = connect(db)
    try:
        if sym == INDEX_SYM:
            df = pd.read_sql("SELECT * FROM prices ORDER BY date", conn, parse_dates=["date"])
        else:
            df = pd.read_sql(
                "SELECT date, open, high, low, close, adj_close, volume FROM stock_prices "
                "WHERE symbol=? ORDER BY date", conn, params=(sym,), parse_dates=["date"])
    finally:
        conn.close()
    return df.set_index("date")


def get_prices(db, sym, tf="D"):
    df = load(db, sym)
    if df.empty:
        return []
    rule = {"D": None, "W": "W-FRI", "M": "MS"}.get(tf)
    if rule:
        df = df.resample(rule).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    df = df.dropna(subset=["open", "high", "low", "close"])
    secs = ((df.index - EPOCH) // pd.Timedelta(seconds=1)).tolist()
    vol = df["volume"].fillna(0).tolist()
    return [
        {"time": int(t), "open": round(float(o), 2), "high": round(float(h), 2),
         "low": round(float(l), 2), "close": round(float(c), 2), "volume": int(v)}
        for t, o, h, l, c, v in zip(secs, df["open"], df["high"], df["low"], df["close"], vol)
    ]


def get_info(db, sym, demo=False):
    sym = norm(sym)
    conn = connect(db)
    try:
        if sym == INDEX_SYM:
            q, args = "SELECT COUNT(*), MIN(date), MAX(date) FROM prices", ()
        else:
            q, args = ("SELECT COUNT(*), MIN(date), MAX(date) FROM stock_prices WHERE symbol=?", (sym,))
        n, a, b = conn.execute(q, args).fetchone()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"fetched:{sym}",)).fetchone()
    finally:
        conn.close()
    return {"rows": n, "first": a, "last": b, "fetched": float(row[0]) if row else None,
            "demo": demo}


# ----------------------------------------------------------------- drawings
def _dkey(sym):
    return "drawings" if sym == INDEX_SYM else f"drawings:{sym}"


def get_drawings(db, sym):
    conn = connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (_dkey(norm(sym)),)).fetchone()
    finally:
        conn.close()
    import json
    return json.loads(row[0]) if row else []


def put_drawings(db, sym, items):
    import json
    conn = connect(db)
    try:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (_dkey(norm(sym)), json.dumps(items)))
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- universe
def universe(db):
    """[(symbol, name), ...] sorted by symbol."""
    conn = connect(db)
    try:
        return conn.execute("SELECT symbol, COALESCE(name,'') FROM stocks ORDER BY symbol").fetchall()
    finally:
        conn.close()


def _add_stocks(db, items, overwrite_names=True):
    conn = connect(db)
    try:
        for sym, name in items:
            sym = norm(sym)
            if sym == INDEX_SYM:
                continue
            if overwrite_names and name:
                conn.execute(
                    "INSERT INTO stocks VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name",
                    (sym, name))
            else:
                conn.execute("INSERT OR IGNORE INTO stocks VALUES (?,?)", (sym, name or ""))
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
    finally:
        conn.close()


def load_fallback(db):
    """Bundled starter list (about 150 well-known IDX stocks)."""
    df = pd.read_csv(HERE / "idx_stocks.csv")
    return _add_stocks(db, [(c, n) for c, n in zip(df["code"], df["name"])], overwrite_names=False)


def refresh_universe(db):
    """Ask Yahoo's screener for every stock listed on the Jakarta exchange (JKT)."""
    import yfinance as yf
    q = yf.EquityQuery("eq", ["exchange", "JKT"])
    found, offset = {}, 0
    while offset < 3000:
        res = yf.screen(q, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False)
        quotes = (res or {}).get("quotes", [])
        for x in quotes:
            s = x.get("symbol", "")
            if s.endswith(".JK"):
                found[s] = x.get("longName") or x.get("shortName") or ""
        if len(quotes) < 250:
            break
        offset += 250
    if not found:
        raise RuntimeError("Yahoo screener returned no Indonesian stocks")
    return _add_stocks(db, list(found.items()))


def import_codes(db, df):
    """Import a stock list (e.g. IDX 'Daftar Saham' Excel/CSV). Needs a code column."""
    cols = {str(c).strip().lower(): c for c in df.columns}
    code_col = next((cols[k] for k in ("kode", "code", "symbol", "ticker", "kode saham", "stock code") if k in cols), None)
    if code_col is None:
        raise ValueError("No code column found. Expected a column named Kode / Code / Symbol / Ticker.")
    name_col = next((cols[k] for k in ("nama perusahaan", "company name", "name", "nama", "nama emiten") if k in cols), None)
    items = []
    for i, c in enumerate(df[code_col]):
        c = str(c).strip().upper()
        if 2 <= len(c) <= 6 and c.isalnum():
            items.append((c, str(df[name_col].iloc[i]).strip() if name_col else ""))
    if not items:
        raise ValueError("The file has no usable stock codes.")
    return _add_stocks(db, items)
