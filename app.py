#!/usr/bin/env python3
"""
IHSG interactive chart server (TradingView-style UI with drawing tools).

    pip install yfinance pandas matplotlib
    python app.py                 # opens http://127.0.0.1:8000
    python app.py --demo          # synthetic data, no internet needed
    python app.py --db ../ihsg.db # reuse the database made by ihsg_db.py

Put this folder next to ihsg_db.py (or keep it as-is: a copy is included here).
Only the Python standard library + pandas (+ yfinance for refreshing) are needed.
"""
import argparse
import json
import sqlite3
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import ihsg_db as core  # noqa: E402

CFG = {"db": "ihsg.db", "start": "1997-07-01", "demo": False}
EPOCH = pd.Timestamp("1970-01-01")


def get_prices(tf):
    conn = core.connect(CFG["db"])
    try:
        df = core.load(conn)
    finally:
        conn.close()
    if df.empty:
        return []
    rule = {"D": None, "W": "W-FRI", "M": "MS"}.get(tf)
    if rule:
        df = df.resample(rule).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
    df = df.dropna(subset=["open", "high", "low", "close"])
    secs = ((df.index - EPOCH) // pd.Timedelta(seconds=1)).tolist()
    vol = df["volume"].fillna(0).tolist()
    return [
        {
            "time": int(t),
            "open": round(float(o), 2),
            "high": round(float(h), 2),
            "low": round(float(l), 2),
            "close": round(float(c), 2),
            "volume": int(v),
        }
        for t, o, h, l, c, v in zip(
            secs, df["open"], df["high"], df["low"], df["close"], vol
        )
    ]


def get_info():
    conn = core.connect(CFG["db"])
    try:
        n, a, b = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM prices").fetchone()
        row = conn.execute("SELECT value FROM meta WHERE key='last_update'").fetchone()
    finally:
        conn.close()
    return {"rows": n, "first": a, "last": b, "updated": row[0] if row else None,
            "demo": CFG["demo"]}


def do_refresh():
    conn = core.connect(CFG["db"])
    try:
        last = core.last_date(conn)
        start = (
            (pd.Timestamp(last) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
            if last else CFG["start"]
        )
        df = core.fake_data() if CFG["demo"] else core.fetch_yfinance(start)
        if df is None or df.empty:
            raise RuntimeError("yfinance returned no data (check internet / try: pip install -U yfinance)")
        n = core.upsert(conn, df)
    finally:
        conn.close()
    return n


def get_drawings():
    conn = core.connect(CFG["db"])
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='drawings'").fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else []


def put_drawings(items):
    conn = core.connect(CFG["db"])
    try:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('drawings', ?)", (json.dumps(items),))
        conn.commit()
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/lightweight-charts.js":
                self._send(200, (HERE / "lightweight-charts.js").read_bytes(),
                           "application/javascript")
            elif u.path == "/api/prices":
                tf = parse_qs(u.query).get("tf", ["D"])[0]
                self._send(200, {"bars": get_prices(tf)})
            elif u.path == "/api/info":
                self._send(200, get_info())
            elif u.path == "/api/drawings":
                self._send(200, get_drawings())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            try:
                self._send(200, {"ok": True, "rows": do_refresh()})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self):
        if urlparse(self.path).path == "/api/drawings":
            try:
                n = int(self.headers.get("Content-Length", 0))
                put_drawings(json.loads(self.rfile.read(n) or b"[]"))
                self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"error": "not found"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="ihsg.db")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--start", default="1997-07-01")
    p.add_argument("--demo", action="store_true", help="synthetic data (offline test)")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    CFG.update(db=a.db, start=a.start, demo=a.demo)

    conn = core.connect(a.db)
    empty = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    conn.close()
    if empty and a.demo:
        do_refresh()
        print("Seeded demo data.")
    elif empty:
        print("Database is empty - click 'Refresh data' in the page to download from yfinance.")

    url = f"http://127.0.0.1:{a.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"IHSG chart running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
