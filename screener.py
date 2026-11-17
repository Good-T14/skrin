"""
Technical screener for the IDX universe (no Streamlit code in here, so it is easy to test).

Flow:  download_universe()  ->  stock_prices table (about 2 years of daily bars per stock)
       build_snapshot()     ->  one row of indicators per stock, stored in table `snapshot`
       apply_filters()      ->  filter the snapshot instantly (presets or custom filters)
"""
import time

import numpy as np
import pandas as pd

import data

PERIODS = {"Q": "Quarterly", "Y": "Yearly", "M": "Monthly", "W": "Weekly"}
MIN_BARS = 30
OHLC = ["Open", "High", "Low", "Close", "AdjClose", "Volume"]


# ===================================================================== download
def _normalize(raw):
    df = raw.rename(columns={"Adj Close": "AdjClose"}).copy()
    if "Close" not in df:
        return pd.DataFrame()
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    df = df.dropna(subset=["Close"])
    for c in ("Open", "High", "Low"):
        df[c] = df[c].fillna(df["Close"]) if c in df else df["Close"]
    df["AdjClose"] = df["AdjClose"].fillna(df["Close"]) if "AdjClose" in df else df["Close"]
    df["Volume"] = df["Volume"].fillna(0) if "Volume" in df else 0
    return df[OHLC]


def _split(raw, batch):
    """yf.download result -> {symbol: normalized frame}, whatever the column layout."""
    out = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        lv0 = set(raw.columns.get_level_values(0))
        if "Close" in lv0 and not (lv0 & set(batch)):
            raw = raw.swaplevel(axis=1)
        for t in batch:
            if t in set(raw.columns.get_level_values(0)):
                df = _normalize(raw[t])
                if not df.empty:
                    out[t] = df
    else:
        df = _normalize(raw)
        if not df.empty:
            out[batch[0]] = df
    return out


def _yf_chunk(conn, chunk, years):
    import yfinance as yf
    lasts = {s: data._last_date(conn, s) for s in chunk}
    have = [s for s in chunk if lasts[s]]
    new = [s for s in chunk if not lasts[s]]
    frames = {}
    kw = dict(interval="1d", auto_adjust=False, group_by="ticker", progress=False, threads=True)
    if new:
        frames.update(_split(yf.download(new, period=f"{years}y", **kw), new))
    if have:
        start = (pd.Timestamp(min(lasts[s] for s in have)) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        frames.update(_split(yf.download(have, start=start, **kw), have))
    return frames


def download_universe(db, symbols, demo=False, progress=None, batch=40, fresh_hours=6, years=2):
    """Download ~2 years of daily bars for many stocks, `batch` at a time.
    Resumable: stocks fetched within `fresh_hours` are skipped, and every batch is saved as it
    arrives, so an interrupted scan keeps its progress. Returns a stats dict."""
    conn = data.connect(db)
    now = time.time()
    todo = []
    try:
        for s in symbols:
            r = conn.execute("SELECT value FROM meta WHERE key=?", (f"fetched:{s}",)).fetchone()
            if r and now - float(r[0]) < fresh_hours * 3600 and data._last_date(conn, s):
                continue
            todo.append(s)
        stats = {"requested": len(symbols), "skipped_fresh": len(symbols) - len(todo), "ok": 0, "failed": []}
        total = max(1, len(todo))
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            try:
                if demo:
                    since = (pd.Timestamp.today() - pd.Timedelta(days=365 * years)).strftime("%Y-%m-%d")
                    frames = {s: data._fake(s, since) for s in chunk}
                else:
                    frames = _yf_chunk(conn, chunk, years)
            except Exception:  # noqa: BLE001  (rate limit / network): keep going with the next batch
                frames = {}
            for s, df in frames.items():
                data._upsert(conn, s, df)
                conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"fetched:{s}", str(time.time())))
                stats["ok"] += 1
            conn.commit()
            stats["failed"] += [s for s in chunk if s not in frames]
            if progress:
                done = min(len(todo), i + len(chunk))
                progress(done / total, f"Downloading prices... {done} of {len(todo)} stocks")
            if not demo:
                time.sleep(0.4)
        return stats
    finally:
        conn.close()


# =================================================================== indicators
def _period_key(idx, per):
    if per == "Y":
        return np.asarray(idx.year)
    if per == "Q":
        return np.asarray(idx.year) * 4 + (np.asarray(idx.month) - 1) // 3
    if per == "M":
        return np.asarray(idx.year) * 12 + np.asarray(idx.month) - 1
    return (np.asarray((idx - pd.Timestamp("1970-01-01")).days) + 3) // 7  # weeks starting Monday


def vwap_bands(d, per):
    """Anchored VWAP that restarts every `per` period, and its volume-weighted std-dev.
    Same maths as the chart: source = (H+L+C)/3."""
    tp = (d["high"] + d["low"] + d["close"]) / 3
    w = d["volume"].astype(float)
    if w.sum() <= 0:                      # feed without volume: weight bars equally
        w = pd.Series(1.0, index=d.index)
    key = pd.Series(_period_key(d.index, per), index=d.index)
    cv = w.groupby(key).cumsum()
    cpv = (w * tp).groupby(key).cumsum()
    cpv2 = (w * tp * tp).groupby(key).cumsum()
    cv = cv.where(cv > 0)
    vwap = cpv / cv
    sd = np.sqrt((cpv2 / cv - vwap ** 2).clip(lower=0))
    return vwap, sd


def _bars_since(cond, limit):
    a = np.asarray(cond, dtype=bool)[-limit:]
    idx = np.flatnonzero(a)
    return float(len(a) - 1 - idx[-1]) if len(idx) else np.nan


def compute_row(d):
    """All indicators for one stock (d: DataFrame with open/high/low/close/volume)."""
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"].astype(float)
    n, last = len(d), float(d["close"].iloc[-1])
    r = {"last_date": d.index[-1], "bars": n, "close": last, "volume": float(v.iloc[-1])}

    def chg(k):
        prev = float(c.iloc[-1 - k]) if n > k else np.nan
        return (last / prev - 1) * 100 if prev and prev > 0 else np.nan
    r.update(chg_1d=chg(1), chg_5d=chg(5), chg_1m=chg(21), chg_3m=chg(63), chg_6m=chg(126))

    av = v.rolling(20).mean()
    r["avg_vol20"] = float(av.iloc[-1]) if pd.notna(av.iloc[-1]) else np.nan
    r["rvol"] = float(v.iloc[-1] / av.iloc[-1]) if av.iloc[-1] and av.iloc[-1] > 0 else np.nan
    r["value20_bn"] = float((c * v).rolling(20).mean().iloc[-1] / 1e9)

    sma = {k: c.rolling(k).mean() for k in (20, 50, 200)}
    for k, s in sma.items():
        r[f"sma{k}"] = float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else np.nan
        r[f"vs_sma{k}"] = (last / s.iloc[-1] - 1) * 100 if pd.notna(s.iloc[-1]) else np.nan
    diff = sma[50] - sma[200]
    r["ma_up"] = np.nan if pd.isna(diff.iloc[-1]) else float(diff.iloc[-1] > 0)
    r["gc_days"] = _bars_since((diff > 0) & (diff.shift(1) <= 0), 60)
    r["dc_days"] = _bars_since((diff < 0) & (diff.shift(1) >= 0), 60)

    dlt = c.diff()
    au = dlt.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    ad = (-dlt.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100 - 100 / (1 + au / ad)
    r["rsi"] = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else np.nan

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    r["macd_hist"] = float(hist.iloc[-1])
    r["mu_days"] = _bars_since((hist > 0) & (hist.shift(1) <= 0), 20)
    r["md_days"] = _bars_since((hist < 0) & (hist.shift(1) >= 0), 20)

    sd20 = c.rolling(20).std(ddof=0)
    width = (4 * sd20) / sma[20]
    wv = width.dropna().iloc[-120:]
    r["bb_rank"] = float((wv <= wv.iloc[-1]).mean() * 100) if len(wv) >= 30 else np.nan

    if n >= 120:
        hi, lo = float(h.iloc[-252:].max()), float(l.iloc[-252:].min())
        r["from_hi"] = (last / hi - 1) * 100
        r["from_lo"] = (last / lo - 1) * 100
        prior = h.shift(1).rolling(252, min_periods=120).max()
        r["hi_days"] = _bars_since(c >= prior, 20)
    else:
        r.update(from_hi=np.nan, from_lo=np.nan, hi_days=np.nan)

    for per in PERIODS:
        vw, sd = vwap_bands(d, per)
        lv, ls = vw.iloc[-1], sd.iloc[-1]
        ok = pd.notna(lv) and lv > 0
        r[f"vwap_{per}"] = float(lv) if ok else np.nan
        r[f"vd_{per}"] = (last / lv - 1) * 100 if ok else np.nan
        r[f"z_{per}"] = (last - lv) / ls if ok and pd.notna(ls) and ls > 0 else np.nan
        pv = vw.shift(1)
        prev_above, prev_below = (c.shift(1) > pv), (c.shift(1) < pv)
        r[f"xu_{per}"] = _bars_since((c > vw) & ~prev_above & pv.notna(), 20)
        r[f"xd_{per}"] = _bars_since((c < vw) & ~prev_below & pv.notna(), 20)
    return r


# ================================================================== snapshot
def _load_prices(db, days=900):
    conn = data.connect(db)
    try:
        mx = conn.execute("SELECT MAX(date) FROM stock_prices").fetchone()[0]
        if not mx:
            return pd.DataFrame()
        since = (pd.Timestamp(mx) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        return pd.read_sql(
            "SELECT symbol, date, open, high, low, close, volume FROM stock_prices "
            "WHERE date >= ? ORDER BY symbol, date", conn, params=(since,), parse_dates=["date"])
    finally:
        conn.close()


def build_snapshot(db, progress=None):
    px = _load_prices(db)
    if px.empty:
        return pd.DataFrame()
    names = dict(data.universe(db))
    rows, groups = [], px.groupby("symbol")
    total = max(1, groups.ngroups)
    for i, (sym, g) in enumerate(groups):
        d = g.set_index("date")[["open", "high", "low", "close", "volume"]].dropna(subset=["close", "high", "low"])
        d["open"] = d["open"].fillna(d["close"])
        if len(d) < MIN_BARS:
            continue
        r = compute_row(d)
        r.update(symbol=sym, name=names.get(sym, ""))
        rows.append(r)
        if progress and i % 20 == 0:
            progress(i / total, f"Calculating indicators... {i} of {total}")
    snap = pd.DataFrame(rows)
    if snap.empty:
        return snap
    snap["stale_days"] = (snap["last_date"].max() - snap["last_date"]).dt.days
    save_snapshot(db, snap)
    return snap


def save_snapshot(db, snap):
    conn = data.connect(db)
    try:
        out = snap.assign(last_date=snap["last_date"].dt.strftime("%Y-%m-%d"))
        out.to_sql("snapshot", conn, if_exists="replace", index=False)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('snapshot_at', ?)", (str(time.time()),))
        conn.commit()
    finally:
        conn.close()


def snapshot_info(db):
    conn = data.connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='snapshot_at'").fetchone()
        try:
            n, last = conn.execute("SELECT COUNT(*), MAX(last_date) FROM snapshot").fetchone()
        except Exception:  # noqa: BLE001
            n, last = 0, None
    finally:
        conn.close()
    return {"at": float(row[0]) if row else None, "n": n, "last": last}


def load_snapshot(db):
    conn = data.connect(db)
    try:
        return pd.read_sql("SELECT * FROM snapshot", conn, parse_dates=["last_date"])
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    finally:
        conn.close()


# ===================================================================== filters
FILTER_DEFAULTS = dict(
    min_value_bn=1.0, min_price=50, active_days=7,
    sma20="any", sma50="any", sma200="any",
    ma_state="any", ma_cross="any", ma_cross_days=10,
    rsi=(0, 100), macd="any", macd_days=5, chg_1m=(-50, 100),
    rvol_min=0.0, hi_within=100, hi_days=0, squeeze=False,
    vwap_period="Q", vwap_pos="any", vwap_days=5,
)

VWAP_POS = {
    "any": "Any position",
    "above": "Above VWAP",
    "below": "Below VWAP",
    "above_u1": "Above the +1σ band",
    "above_u2": "Above the +2σ band",
    "below_l1": "Below the −1σ band",
    "below_l2": "Below the −2σ band",
    "inside": "Inside the ±1σ bands",
    "cross_up": "Just crossed above VWAP",
    "cross_dn": "Just crossed below VWAP",
}
MACD_OPTS = {"any": "Any", "hist_pos": "Histogram above zero", "hist_neg": "Histogram below zero",
             "bull_cross": "Bullish cross (recent)", "bear_cross": "Bearish cross (recent)"}
SMA_OPTS = {"any": "Any", "above": "Price above", "below": "Price below"}
MA_STATE = {"any": "Any", "up": "SMA 50 above SMA 200", "down": "SMA 50 below SMA 200"}
MA_CROSS = {"any": "Any", "golden": "Golden cross (50 up through 200)", "death": "Death cross (50 down through 200)"}

PRESETS = {
    "Momentum above VWAP": ("Price is above the +1σ band of the quarterly VWAP and above its 50-day average: buyers have been in control since the start of the quarter.",
                            dict(vwap_period="Q", vwap_pos="above_u1", sma50="above", rvol_min=1.0)),
    "VWAP reclaim": ("Price just crossed back above the quarterly VWAP on above-average volume.",
                     dict(vwap_period="Q", vwap_pos="cross_up", vwap_days=5, rvol_min=1.2)),
    "Stretched below VWAP": ("Price is 2σ or more below the yearly VWAP and RSI is weak: possible oversold bounce candidates (or falling knives, so check the chart).",
                             dict(vwap_period="Y", vwap_pos="below_l2", rsi=(0, 40))),
    "Stretched above VWAP": ("Price is 2σ or more above the yearly VWAP: extended, watch for pullbacks.",
                             dict(vwap_period="Y", vwap_pos="above_u2")),
    "Uptrend pullback": ("Long-term uptrend (above SMA 200, SMA 50 above SMA 200) that has dipped below its 20-day average with a cooled-off RSI.",
                         dict(sma200="above", ma_state="up", sma20="below", rsi=(35, 55))),
    "Golden cross": ("The 50-day average crossed up through the 200-day average in the last 10 days.",
                     dict(ma_cross="golden", ma_cross_days=10)),
    "52-week high breakout": ("Closed at a new 52-week high within the last 5 days on above-average volume.",
                              dict(hi_days=5, rvol_min=1.5)),
    "Volume spike": ("Today's volume is at least 2.5 times the 20-day average.",
                     dict(rvol_min=2.5)),
    "Oversold + MACD turn": ("RSI is low and the MACD histogram just turned positive: momentum may be turning up.",
                             dict(rsi=(0, 35), macd="bull_cross", macd_days=5)),
    "Bollinger squeeze": ("Volatility is unusually low versus the last 6 months (Bollinger bands very narrow) in a stock above its SMA 200: a bigger move may be coming.",
                          dict(squeeze=True, sma200="above")),
}


def apply_filters(df, f):
    """Returns (mask, parts) where parts = [(label, mask_of_that_filter_alone), ...]."""
    f = {**FILTER_DEFAULTS, **f}
    parts = []

    def add(cond, label):
        parts.append((label, cond.fillna(False).astype(bool)))

    if f["active_days"] is not None:
        add(df["stale_days"] <= f["active_days"], f"Traded in the last {f['active_days']} days")
    if f["min_value_bn"]:
        add(df["value20_bn"] >= f["min_value_bn"], f"Avg traded value ≥ Rp {f['min_value_bn']:g} bn/day")
    if f["min_price"]:
        add(df["close"] >= f["min_price"], f"Price ≥ Rp {f['min_price']:g}")
    for k in (20, 50, 200):
        if f[f"sma{k}"] != "any":
            up = f[f"sma{k}"] == "above"
            add(df[f"vs_sma{k}"] > 0 if up else df[f"vs_sma{k}"] < 0, f"Price {'above' if up else 'below'} SMA {k}")
    if f["ma_state"] != "any":
        up = f["ma_state"] == "up"
        add(df["ma_up"] == (1.0 if up else 0.0), "SMA 50 above SMA 200" if up else "SMA 50 below SMA 200")
    if f["ma_cross"] != "any":
        col = "gc_days" if f["ma_cross"] == "golden" else "dc_days"
        add(df[col] <= f["ma_cross_days"], f"{'Golden' if f['ma_cross'] == 'golden' else 'Death'} cross in last {f['ma_cross_days']} days")
    lo, hi = f["rsi"]
    if lo > 0 or hi < 100:
        add((df["rsi"] >= lo) & (df["rsi"] <= hi), f"RSI between {lo} and {hi}")
    if f["macd"] != "any":
        m = f["macd"]
        cond = {"hist_pos": df["macd_hist"] > 0, "hist_neg": df["macd_hist"] < 0,
                "bull_cross": df["mu_days"] <= f["macd_days"], "bear_cross": df["md_days"] <= f["macd_days"]}[m]
        extra = f" (last {f['macd_days']} days)" if "cross" in m else ""
        add(cond, f"MACD: {MACD_OPTS[m].lower()}{extra}")
    lo, hi = f["chg_1m"]
    if lo > -50 or hi < 100:
        c = pd.Series(True, index=df.index)
        if lo > -50:
            c &= df["chg_1m"] >= lo
        if hi < 100:
            c &= df["chg_1m"] <= hi
        add(c, f"1-month change {lo if lo > -50 else '…'}% to {hi if hi < 100 else '…'}%")
    if f["rvol_min"]:
        add(df["rvol"] >= f["rvol_min"], f"Relative volume ≥ {f['rvol_min']:g}×")
    if f["hi_within"] < 100:
        add(df["from_hi"] >= -f["hi_within"], f"Within {f['hi_within']}% of 52-week high")
    if f["hi_days"]:
        add(df["hi_days"] <= f["hi_days"], f"New 52-week high in last {f['hi_days']} days")
    if f["squeeze"]:
        add(df["bb_rank"] <= 20, "Bollinger squeeze")
    P = f["vwap_period"]
    pos = f["vwap_pos"]
    if pos != "any":
        z, vd = df[f"z_{P}"], df[f"vd_{P}"]
        cond = {"above": vd > 0, "below": vd < 0, "above_u1": z >= 1, "above_u2": z >= 2,
                "below_l1": z <= -1, "below_l2": z <= -2, "inside": (z > -1) & (z < 1),
                "cross_up": df[f"xu_{P}"] <= f["vwap_days"], "cross_dn": df[f"xd_{P}"] <= f["vwap_days"]}[pos]
        extra = f" (last {f['vwap_days']} days)" if pos.startswith("cross") else ""
        add(cond, f"{PERIODS[P]} VWAP: {VWAP_POS[pos].lower()}{extra}")
    mask = pd.Series(True, index=df.index)
    for _, m in parts:
        mask &= m
    return mask, parts
