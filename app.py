#!/usr/bin/env python3
"""
IHSG + all IDX stocks - standalone chart server (TradingView-style UI).

    pip install yfinance pandas matplotlib
    python app.py                 # opens http://127.0.0.1:8000
    python app.py --demo          # synthetic data, no internet needed

If this file is used as the *Streamlit* main file it hands over to streamlit_app.py,
so both `python app.py` and `streamlit run app.py` work.
"""
import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import data  # noqa: E402

CFG = {"db": "ihsg.db", "demo": False}


def symbols():
    out = [{"symbol": data.INDEX_SYM, "label": "IHSG", "name": "IDX Composite"}]
    out += [{"symbol": s, "label": data.label(s), "name": n} for s, n in data.universe(CFG["db"])]
    return out


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

    def _sym(self, q):
        return data.norm(q.get("sym", [""])[0])

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        db, demo = CFG["db"], CFG["demo"]
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/lightweight-charts.js":
                self._send(200, (HERE / "lightweight-charts.js").read_bytes(), "application/javascript")
            elif u.path == "/api/symbols":
                self._send(200, symbols())
            elif u.path == "/api/prices":
                sym, tf = self._sym(q), q.get("tf", ["D"])[0]
                try:
                    data.update_symbol(db, sym, demo)          # throttled to once an hour
                except Exception as e:  # noqa: BLE001
                    if not data.get_info(db, sym)["rows"]:
                        self._send(500, {"error": str(e)})
                        return
                names = dict(data.universe(db))
                self._send(200, {"bars": data.get_prices(db, sym, tf),
                                 "symbol": sym, "label": data.label(sym),
                                 "name": "IDX Composite" if sym == data.INDEX_SYM else names.get(sym, "")})
            elif u.path == "/api/info":
                self._send(200, data.get_info(db, self._sym(q), demo))
            elif u.path == "/api/drawings":
                self._send(200, data.get_drawings(db, self._sym(q)))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/refresh":
                n = data.update_symbol(CFG["db"], self._sym(q), CFG["demo"], force=True)
                self._send(200, {"ok": True, "rows": n})
            elif u.path == "/api/universe":
                n = data.refresh_universe(CFG["db"])
                self._send(200, {"ok": True, "count": n})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(e)})

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path == "/api/drawings":
            try:
                n = int(self.headers.get("Content-Length", 0))
                data.put_drawings(CFG["db"], self._sym(parse_qs(u.query)), json.loads(self.rfile.read(n) or b"[]"))
                self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(e)})
        else:
            self._send(404, {"error": "not found"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="ihsg.db")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--demo", action="store_true", help="synthetic data (offline test)")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()
    CFG.update(db=a.db, demo=a.demo)

    if len(data.universe(a.db)) == 0:
        data.load_fallback(a.db)
    if not a.demo and len(data.universe(a.db)) < data.FULL_UNIVERSE_MIN:
        def _bg():
            try:
                n = data.refresh_universe(a.db)
                print(f"Stock list updated from Yahoo: {n} stocks.")
            except Exception as e:  # noqa: BLE001
                print(f"Could not fetch the full stock list ({e}); using the starter list.")
        threading.Thread(target=_bg, daemon=True).start()

    url = f"http://127.0.0.1:{a.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"IHSG chart running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def _under_streamlit():
    try:
        from streamlit.runtime import exists
        return exists()
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    if _under_streamlit():
        import runpy
        runpy.run_path(str(HERE / "streamlit_app.py"), run_name="__streamlit_ui__")
    else:
        main()
