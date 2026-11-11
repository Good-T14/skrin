#!/usr/bin/env python3
"""
IHSG / JKSE database + charts, from yfinance.

Usage:
    pip install yfinance pandas matplotlib
    python ihsg_db.py                      # full history, builds ihsg.db + charts/
    python ihsg_db.py --start 2015-01-01   # limit history
    python ihsg_db.py                      # run again later: only fetches new rows
    python ihsg_db.py --no-fetch           # just redraw charts from the database
    python ihsg_db.py --demo               # synthetic data (offline test only)
"""
import argparse
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TICKER = "^JKSE"
TRADING_DAYS = 245  # approx. IDX trading days per year

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    date      TEXT PRIMARY KEY,   -- YYYY-MM-DD
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    adj_close REAL,
    volume    INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
DROP VIEW IF EXISTS v_daily;
CREATE VIEW v_daily AS
SELECT date, open, high, low, close, volume,
       close / LAG(close) OVER (ORDER BY date) - 1 AS daily_return
FROM prices;
"""


# ---------- database ----------
def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def last_date(conn):
    row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    return row[0]


def upsert(conn, df):
    rows = [
        (d.strftime("%Y-%m-%d"), r.Open, r.High, r.Low, r.Close, r.AdjClose, int(r.Volume))
        for d, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?)", rows
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('last_update', datetime('now'))"
    )
    conn.commit()
    return len(rows)


def load(conn):
    df = pd.read_sql("SELECT * FROM prices ORDER BY date", conn, parse_dates=["date"])
    return df.set_index("date")


# ---------- data ----------
def fetch_yfinance(start, symbol=TICKER):
    import yfinance as yf
    raw = yf.Ticker(symbol).history(start=start, auto_adjust=False)
    if raw.empty:
        return raw
    raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
    df = raw.rename(columns={"Adj Close": "AdjClose"})
    df = df[["Open", "High", "Low", "Close", "AdjClose", "Volume"]]
    df = df.dropna(subset=["Close"])
    df["Volume"] = df["Volume"].fillna(0)
    return df


def fake_data(start="2015-01-01", seed=1, base=4500):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, pd.Timestamp.today().normalize())
    close = base * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(idx))))
    df = pd.DataFrame(index=idx)
    df["Close"] = close
    df["AdjClose"] = close
    df["Open"] = close * (1 + rng.normal(0, 0.002, len(idx)))
    df["High"] = np.maximum(df.Open, df.Close) * 1.004
    df["Low"] = np.minimum(df.Open, df.Close) * 0.996
    df["Volume"] = rng.integers(1e8, 5e8, len(idx))
    return df


# ---------- charts ----------
def save(fig, outdir, name):
    fig.tight_layout()
    fig.savefig(outdir / name, dpi=150)
    plt.close(fig)


def make_charts(df, outdir):
    outdir.mkdir(exist_ok=True)
    c = df["close"]
    ret = c.pct_change().dropna()

    # 1. Price + SMAs + volume
    fig, (a1, a2) = plt.subplots(
        2, 1, figsize=(13, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    a1.plot(c, lw=1, label="IHSG close", color="#1f3a5f")
    for n, col in [(50, "#e69f00"), (200, "#d55e00")]:
        a1.plot(c.rolling(n).mean(), lw=1, label=f"SMA {n}", color=col)
    a1.set_title("IHSG (^JKSE) - Price with 50/200-day moving averages")
    a1.legend(loc="upper left")
    a1.grid(alpha=0.3)
    a2.bar(df.index, df["volume"], width=1.0, color="#7a8ba3")
    a2.set_ylabel("Volume")
    a2.grid(alpha=0.3)
    save(fig, outdir, "1_price_volume.png")

    # 2. Drawdown
    dd = c / c.cummax() - 1
    fig, ax = plt.subplots(figsize=(13, 4))
    ax.fill_between(dd.index, dd * 100, 0, color="#c0392b", alpha=0.6)
    ax.set_title(f"IHSG drawdown from all-time high (max {dd.min():.1%})")
    ax.set_ylabel("%")
    ax.grid(alpha=0.3)
    save(fig, outdir, "2_drawdown.png")

    # 3. Daily return distribution
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(ret * 100, bins=120, color="#1f3a5f", alpha=0.85)
    ax.set_title(f"Daily return distribution (std {ret.std():.2%})")
    ax.set_xlabel("Daily return, %")
    ax.set_xlim(-6, 6)
    ax.grid(alpha=0.3)
    save(fig, outdir, "3_return_hist.png")

    # 4. Rolling annualised volatility
    vol = ret.rolling(30).std() * np.sqrt(TRADING_DAYS) * 100
    fig, ax = plt.subplots(figsize=(13, 4))
    ax.plot(vol, lw=1, color="#8e44ad")
    ax.set_title("30-day rolling annualised volatility")
    ax.set_ylabel("%")
    ax.grid(alpha=0.3)
    save(fig, outdir, "4_rolling_vol.png")

    # 5. Calendar-year returns
    yearly = c.resample("YE").last().pct_change().dropna() * 100
    first = c.iloc[0]
    first_year = c.index[0].year
    if first_year not in (yearly.index.year):
        # partial first year: from first close to its year-end
        ye = c[c.index.year == first_year].iloc[-1]
        yearly.loc[pd.Timestamp(f"{first_year}-12-31")] = (ye / first - 1) * 100
        yearly = yearly.sort_index()
    fig, ax = plt.subplots(figsize=(13, 4.5))
    ax.bar(yearly.index.year, yearly.values,
           color=np.where(yearly.values >= 0, "#27ae60", "#c0392b"))
    for x, v in zip(yearly.index.year, yearly.values):
        ax.text(x, v + (1 if v >= 0 else -3), f"{v:.0f}%", ha="center", fontsize=8)
    ax.set_title("Calendar-year returns (current year is year-to-date)")
    ax.set_ylabel("%")
    ax.grid(alpha=0.3, axis="y")
    save(fig, outdir, "5_yearly_returns.png")

    # 6. Monthly returns heatmap
    m = c.resample("ME").last().pct_change() * 100
    tbl = (
        m.to_frame("r")
        .assign(y=m.index.year, mo=m.index.month)
        .pivot(index="y", columns="mo", values="r")
        .reindex(columns=range(1, 13))
    )
    fig, ax = plt.subplots(figsize=(12, max(4, len(tbl) * 0.4)))
    lim = np.nanpercentile(np.abs(tbl.values), 95)
    im = ax.imshow(tbl.values, cmap="RdYlGn", vmin=-lim, vmax=lim, aspect="auto")
    ax.set_xticks(range(12))
    ax.set_xticklabels(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])
    ax.set_yticks(range(len(tbl)))
    ax.set_yticklabels(tbl.index)
    for i in range(tbl.shape[0]):
        for j in range(12):
            v = tbl.values[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.1f}", ha="center", va="center", fontsize=7)
    ax.set_title("Monthly returns, %")
    fig.colorbar(im, ax=ax, shrink=0.8)
    save(fig, outdir, "6_monthly_heatmap.png")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="ihsg.db")
    p.add_argument("--start", default="1997-07-01", help="history start for first fetch")
    p.add_argument("--charts", default="charts")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--demo", action="store_true", help="synthetic data, no internet")
    a = p.parse_args()

    conn = connect(a.db)
    if not a.no_fetch:
        last = last_date(conn)
        # re-fetch a 7-day overlap so any revised rows get corrected
        start = (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d") if last else a.start
        df = fake_data() if a.demo else fetch_yfinance(start)
        if df.empty:
            print("No data returned from yfinance (check connection / ticker).")
        else:
            n = upsert(conn, df)
            print(f"Upserted {n} rows (from {start}).")

    data = load(conn)
    if data.empty:
        raise SystemExit("Database is empty.")
    print(f"DB: {len(data)} rows, {data.index.min().date()} -> {data.index.max().date()}")
    make_charts(data, Path(a.charts))
    print(f"Charts saved to {a.charts}/")


if __name__ == "__main__":
    main()
