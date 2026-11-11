"""
IHSG interactive chart - Streamlit version.

Local:   streamlit run streamlit_app.py
Cloud:   push this folder to GitHub -> share.streamlit.io -> main file: streamlit_app.py
Offline test:  IHSG_DEMO=1 streamlit run streamlit_app.py
"""
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components  # fallback for old versions

import app as srv  # data layer shared with the standalone server (app.py)

HERE = Path(__file__).resolve().parent
DB = os.environ.get("IHSG_DB", str(HERE / "ihsg.db"))
DEMO = os.environ.get("IHSG_DEMO") == "1"
srv.CFG.update(db=DB, demo=DEMO)

st.set_page_config(page_title="IHSG Chart", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown(
    "<style>.block-container{padding:0.6rem 1rem 0 1rem}header[data-testid=stHeader]{height:0}"
    "footer{visibility:hidden}</style>", unsafe_allow_html=True)


@st.cache_data(ttl=3600, show_spinner="Updating IHSG data from yfinance...")
def auto_update():
    """Runs at most once an hour per server; returns an error string or ''."""
    try:
        srv.do_refresh()
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)


@st.cache_data(show_spinner=False)
def load_payload(version):
    """All three timeframes + info; `version` busts the cache after a refresh."""
    return {"bars": {tf: srv.get_prices(tf) for tf in ("D", "W", "M")}, "info": srv.get_info()}


@st.cache_data(show_spinner=False)
def page_template():
    html = (HERE / "index.html").read_text(encoding="utf-8")
    lib = (HERE / "lightweight-charts.js").read_text(encoding="utf-8")
    return html, lib


# ---------------- sidebar
with st.sidebar:
    st.header("IHSG data")
    height = st.slider("Chart height (px)", 500, 1200, 820, 20)
    auto = st.toggle("Auto-update hourly", value=not DEMO,
                     help="Pulls new daily bars from yfinance when the page is opened.")
    if st.button("Update from yfinance now", use_container_width=True):
        try:
            with st.spinner("Downloading..."):
                n = srv.do_refresh()
            st.cache_data.clear()
            st.success(f"{n} rows fetched")
        except Exception as e:  # noqa: BLE001
            st.error(f"Update failed: {e}")
    st.caption(
        "Drawings are stored in your browser (localStorage), so they persist per browser/device. "
        "The price database lives on the server and is rebuilt from yfinance if it is lost."
    )

# ---------------- data
conn_info = srv.get_info()
if conn_info["rows"] == 0 or auto:
    err = auto_update()
    if err:
        st.warning(f"Could not update from yfinance: {err}")

version = (srv.get_info()["last"], srv.get_info()["rows"])
payload = load_payload(version)
if not payload["bars"]["D"]:
    st.error("No data available. Check the internet connection and press 'Update from yfinance now'.")
    st.stop()

# ---------------- chart
html, lib = page_template()
data_js = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
page = html.replace(
    '<script src="/lightweight-charts.js"></script>',
    f"<script>{lib}</script>\n<script>window.IHSG_DATA={data_js};</script>",
)
if hasattr(st, "iframe"):            # current Streamlit
    st.iframe(page, height=height)
else:                                # older Streamlit versions
    components.html(page, height=height, scrolling=False)
