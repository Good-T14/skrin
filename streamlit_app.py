"""
IHSG + every IDX stock - Streamlit version.

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

HERE = Path(__file__).resolve().parent
DB = os.environ.get("IHSG_DB", str(HERE / "ihsg.db"))
DEMO = os.environ.get("IHSG_DEMO") == "1"

st.set_page_config(page_title="IHSG Chart", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")
# keep Streamlit's header (it holds the sidebar button); just trim the padding around the chart
st.markdown(
    "<style>.block-container{padding:3.2rem 0.8rem 0 0.8rem !important}"
    "footer{visibility:hidden}</style>", unsafe_allow_html=True)


# ------------------------------------------------------------ cached helpers
@st.cache_data(ttl=86400, show_spinner="Loading the list of IDX stocks...")
def ensure_universe(_demo):
    """Fill the stock list (once a day). Returns a status message."""
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
    """Chart payload for one symbol; `version` busts the cache after new data arrives."""
    names = dict(data.universe(DB))
    return {
        "symbol": sym,
        "label": data.label(sym),
        "name": "IDX Composite" if sym == data.INDEX_SYM else names.get(sym, ""),
        "bars": {tf: data.get_prices(DB, sym, tf) for tf in ("D", "W", "M")},
        "info": data.get_info(DB, sym, DEMO),
    }


@st.cache_data(show_spinner=False)
def page_template():
    return ((HERE / "index.html").read_text(encoding="utf-8"),
            (HERE / "lightweight-charts.js").read_text(encoding="utf-8"))


# ------------------------------------------------------------------- sidebar
uni_msg = ensure_universe(DEMO)
uni = data.universe(DB)
names = dict(uni)
options = [data.INDEX_SYM] + [s for s, _ in uni]


def fmt_sym(s):
    if s == data.INDEX_SYM:
        return "IHSG - IDX Composite (^JKSE)"
    return f"{data.label(s)} - {names.get(s, '')}".rstrip(" -")


qp = data.norm(st.query_params.get("sym", data.INDEX_SYM))
if "sym" not in st.session_state:
    st.session_state["sym"] = qp if qp in options else data.INDEX_SYM
# a code that is not in the list (e.g. a brand-new listing) can still be opened via ?sym=XXXX
if st.session_state["sym"] not in options:
    options.append(st.session_state["sym"])

with st.sidebar:
    st.header("Symbol")
    sym = st.selectbox("IHSG or any IDX stock (type to search)", options, key="sym",
                       format_func=fmt_sym)
    st.caption(f"{len(uni):,} stocks in the list")
    st.query_params["sym"] = sym

    st.header("Data")
    auto = st.toggle("Auto-update (hourly)", value=not DEMO,
                     help="Pulls new daily bars from yfinance when you open a symbol.")
    force = st.button("Update this symbol now", use_container_width=True)

    with st.expander("Stock list"):
        st.caption("The list comes from Yahoo's screener (all stocks on the Jakarta exchange). "
                   "If that is blocked, a starter list of ~150 stocks is used. "
                   "You can also upload the official IDX list (Excel/CSV with a 'Kode' column).")
        if st.button("Reload list from Yahoo", use_container_width=True):
            try:
                with st.spinner("Asking Yahoo..."):
                    n = data.refresh_universe(DB)
                ensure_universe.clear()
                st.success(f"{n} stocks in the list")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Failed: {e}")
        up = st.file_uploader("Upload IDX stock list", type=["csv", "xlsx", "xls"])
        if up is not None and st.button("Import list", use_container_width=True):
            try:
                df = pd.read_csv(up) if up.name.lower().endswith(".csv") else pd.read_excel(up)
                n = data.import_codes(DB, df)
                st.success(f"List now has {n} stocks")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Import failed: {e}")
    st.caption("Drawings and indicator settings are stored in your browser (per symbol). "
               "Price data lives on the server and is re-downloaded from yfinance if lost.")

if uni_msg and "Could not" in uni_msg:
    st.sidebar.warning(uni_msg)

# ----------------------------------------------------------------- data load
try:
    if force:
        with st.spinner(f"Downloading {data.label(sym)} from yfinance..."):
            data.update_symbol(DB, sym, DEMO, force=True)
        st.cache_data.clear()
    elif auto or data.get_info(DB, sym)["rows"] == 0:
        with st.spinner(f"Loading {data.label(sym)}..."):
            data.update_symbol(DB, sym, DEMO)
except Exception as e:  # noqa: BLE001
    if data.get_info(DB, sym)["rows"] == 0:
        st.error(f"No data for {fmt_sym(sym)}: {e}")
        st.stop()
    st.warning(f"Could not update {data.label(sym)}: {e}. Showing stored data.")

info = data.get_info(DB, sym, DEMO)
payload = load_payload(sym, (info["last"], info["rows"]))
if not payload["bars"]["D"]:
    st.error("No data available for this symbol.")
    st.stop()

# --------------------------------------------------------------------- chart
html, lib = page_template()
data_js = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
page = html.replace(
    '<script src="/lightweight-charts.js"></script>',
    f"<script>{lib}</script>\n<script>window.IHSG_DATA={data_js};</script>",
)
# The page resizes its own frame to fill the browser window (see fitFrame in index.html);
# the number below is only the initial height.
if hasattr(st, "iframe"):
    st.iframe(page, height=720)
else:
    components.html(page, height=720, scrolling=False)
