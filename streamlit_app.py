"""
IHSG + every IDX stock: interactive chart and technical screener (Streamlit).

Local:   streamlit run streamlit_app.py
Cloud:   push this folder to GitHub -> share.streamlit.io -> main file: streamlit_app.py (or app.py)
Offline test:  IHSG_DEMO=1 streamlit run streamlit_app.py
"""
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components  # fallback for old Streamlit versions

import data
import screener_ui as sui

HERE = Path(__file__).resolve().parent
DB = os.environ.get("IHSG_DB", str(HERE / "ihsg.db"))
DEMO = os.environ.get("IHSG_DEMO") == "1"
stretch = sui.stretch

st.set_page_config(page_title="IHSG Chart & Screener", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(
    "<style>.block-container{padding:3.2rem 0.8rem 0 0.8rem !important}footer{visibility:hidden}"
    "div[data-testid=stSegmentedControl] button{padding:0.35rem 1.1rem}</style>", unsafe_allow_html=True)

# ----------------------------------------------------------- session state
sui.init_state()
# Streamlit forgets widgets that are not drawn in a run; re-assigning keeps the screener
# filters alive while you look at a chart.
for _k in sui.FILTER_KEYS + ["scan_scope", "scan_codes", "preset"]:
    st.session_state[_k] = st.session_state[_k]


# ------------------------------------------------------------ cached helpers
@st.cache_data(ttl=86400, show_spinner="Loading the list of IDX stocks...")
def ensure_universe(_demo):
    msg = ""
    if len(data.universe(DB)) < data.FULL_UNIVERSE_MIN and not _demo:
        try:
            n = data.refresh_universe(DB)
            msg = f"{n} stocks loaded from Yahoo."
        except Exception as e:  # noqa: BLE001
            msg = f"Could not fetch the full list from Yahoo ({e}). Using the starter list."
    if len(data.universe(DB)) == 0:
        data.load_fallback(DB)
    return msg


@st.cache_data(show_spinner=False, max_entries=12)
def load_payload(sym, version):
    names_ = dict(data.universe(DB))
    return {
        "symbol": sym, "label": data.label(sym),
        "name": "IDX Composite" if sym == data.INDEX_SYM else names_.get(sym, ""),
        "bars": {tf: data.get_prices(DB, sym, tf) for tf in ("D", "W", "M")},
        "info": data.get_info(DB, sym, DEMO),
    }


@st.cache_data(show_spinner=False)
def page_template():
    return ((HERE / "index.html").read_text(encoding="utf-8"),
            (HERE / "lightweight-charts.js").read_text(encoding="utf-8"))


uni_msg = ensure_universe(DEMO)
uni = data.universe(DB)
names = dict(uni)
options = [data.INDEX_SYM] + [s for s, _ in uni]


def fmt_sym(s):
    if s == data.INDEX_SYM:
        return "IHSG - IDX Composite (^JKSE)"
    return f"{data.label(s)} - {names.get(s, '')}".rstrip(" -")


if "cur_sym" not in st.session_state:
    st.session_state["cur_sym"] = data.norm(st.query_params.get("sym", data.INDEX_SYM))
st.session_state.setdefault("view", "Chart")
st.session_state.setdefault("results", [])
st.session_state.setdefault("open_vwap", None)


def _on_sym():
    st.session_state["cur_sym"] = st.session_state["sym_box"]
    st.session_state["open_vwap"] = None


def _step(d):
    res, cur = st.session_state["results"], st.session_state["cur_sym"]
    if cur in res:
        nxt = res[(res.index(cur) + d) % len(res)]
        st.session_state["cur_sym"] = nxt
        st.session_state["sym_box"] = nxt


# -------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    auto = st.toggle("Auto-update prices when opening a stock", value=not DEMO, key="auto_update",
                     help="Pulls new daily bars from yfinance (at most once an hour per stock).")
    with st.expander("Stock list"):
        st.caption(f"{len(uni):,} stocks in the list. It comes from Yahoo's screener (all stocks on the Jakarta "
                   "exchange). If that is blocked, a starter list of ~150 stocks is used. You can also upload the "
                   "official IDX list (Excel/CSV with a 'Kode' column).")
        if st.button("Reload list from Yahoo", **stretch(st.button)):
            try:
                with st.spinner("Asking Yahoo..."):
                    n = data.refresh_universe(DB)
                ensure_universe.clear()
                st.success(f"{n} stocks in the list")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Failed: {e}")
        up = st.file_uploader("Upload IDX stock list", type=["csv", "xlsx", "xls"])
        if up is not None and st.button("Import list", **stretch(st.button)):
            try:
                df = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
                n = data.import_codes(DB, df)
                st.success(f"List now has {n} stocks")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Import failed: {e}")
    st.caption("Drawings and chart settings are stored in your browser, per symbol. Price data lives on the server "
               "and is re-downloaded from yfinance if lost.")
    if uni_msg and "Could not" in uni_msg:
        st.warning(uni_msg)

# ------------------------------------------------------------------- top bar
top = st.columns([2.3, 4.2, 2.8, 1.1, 1.1], vertical_alignment="center")
with top[0]:
    st.segmented_control("View", ["Chart", "Screener"], key="view", label_visibility="collapsed")
view = st.session_state.get("view") or "Chart"

with top[4]:
    with st.popover("Help", **stretch(st.popover)):
        st.markdown(
            "**Chart**\n"
            "- Pick a drawing tool on the left, then click the start and end point (or drag). "
            "Click a drawing to select it; drag to move.\n"
            "- `Del` deletes, `Esc` cancels, `Ctrl+Z` undoes. *Magnet* snaps to open/high/low/close.\n"
            "- **Indicators** menu: VWAP (restarts every quarter/year/month/week, ±1σ and ±2σ bands), "
            "moving averages, Bollinger, RSI, MACD.\n"
            "- Drawings and settings are saved in your browser for each stock.\n\n"
            "**Screener**\n"
            "- Download prices once, pick a preset, fine-tune, then open a result in the chart and step "
            "through the results with ◀ ▶.")

# ---------------------------------------------------------------- screener view
if view == "Screener":
    sui.render(DB, DEMO, names)
    st.stop()

# ------------------------------------------------------------------ chart view
sym = st.session_state["cur_sym"]
if sym not in options:
    options.append(sym)
if st.session_state.get("sym_box") != sym:
    st.session_state["sym_box"] = sym
with top[1]:
    st.selectbox("Symbol", options, key="sym_box", format_func=fmt_sym, label_visibility="collapsed",
                 on_change=_on_sym, help="Type to search any IDX stock by code or name.")
st.query_params["sym"] = sym

results = st.session_state["results"]
with top[2]:
    if sym in results and len(results) > 1:
        a, b, c = st.columns([1, 1, 3], vertical_alignment="center")
        a.button("◀", key="prev", on_click=_step, args=(-1,), help="Previous screener result")
        b.button("▶", key="next", on_click=_step, args=(1,), help="Next screener result")
        c.caption(f"{results.index(sym) + 1} of {len(results)} results")
with top[3]:
    force = st.button("Update", help="Download the newest prices for this symbol", **stretch(st.button))

try:
    if force:
        with st.spinner(f"Downloading {data.label(sym)} from yfinance..."):
            data.update_symbol(DB, sym, DEMO, force=True)
        st.cache_data.clear()
    elif auto or data.get_info(DB, sym)["rows"] == 0 or not data.has_full(DB, sym):
        with st.spinner(f"Loading {data.label(sym)} (first time can take a few seconds)..."):
            data.update_symbol(DB, sym, DEMO)
except Exception as e:  # noqa: BLE001
    if data.get_info(DB, sym)["rows"] == 0:
        st.error(f"Could not load {fmt_sym(sym)}: {e}. Check the code, or try again in a moment.")
        st.stop()
    st.warning(f"Could not update {data.label(sym)} ({e}). Showing the stored prices.")

info = data.get_info(DB, sym, DEMO)
payload = load_payload(sym, (info["last"], info["rows"]))
if not payload["bars"]["D"]:
    st.error("No price data for this symbol.")
    st.stop()
if st.session_state.get("open_vwap"):          # opened from a VWAP screen: show the same anchor
    payload = dict(payload, vwap_per=st.session_state["open_vwap"])

html, lib = page_template()
data_js = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
page = html.replace(
    '<script src="/lightweight-charts.js"></script>',
    f"<script>{lib}</script>\n<script>window.IHSG_DATA={data_js};</script>",
)
# the page stretches its own frame to the window height (fitFrame in index.html); 720 is the start value
if hasattr(st, "iframe"):
    st.iframe(page, height=720)
else:
    components.html(page, height=720, scrolling=False)
