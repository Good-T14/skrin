"""Streamlit page: technical screener for IDX stocks."""
import inspect
import time

import numpy as np
import pandas as pd
import streamlit as st

import data
import screener as sc

VALUE_OPTS = [0.0, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0]
FILTER_KEYS = ["f_" + k for k in sc.FILTER_DEFAULTS]


def stretch(fn):
    """Full-width kwargs that work on both new and old Streamlit versions."""
    return {"width": "stretch"} if "width" in inspect.signature(fn).parameters else {"use_container_width": True}


# ------------------------------------------------------------------ state
def init_state():
    for k, v in sc.FILTER_DEFAULTS.items():
        st.session_state.setdefault("f_" + k, v)
    st.session_state.setdefault("preset", None)
    st.session_state.setdefault("scan_scope", "all")
    st.session_state.setdefault("scan_codes", "")


def reset_filters():
    for k, v in sc.FILTER_DEFAULTS.items():
        st.session_state["f_" + k] = v
    st.session_state["preset"] = None


def apply_preset(name):
    reset_filters()
    st.session_state["preset"] = name
    for k, v in sc.PRESETS[name][1].items():
        st.session_state["f_" + k] = v


def goto_chart(sym, results=None, vwap_period=None):
    """Callback: switch to the chart view on `sym`, remembering the result list for prev/next."""
    st.session_state["cur_sym"] = sym
    st.session_state["sym_box"] = sym
    st.session_state["view"] = "Chart"
    st.session_state["results"] = list(results or [])
    st.session_state["open_vwap"] = vwap_period


def _ago(ts):
    if not ts:
        return "never"
    s = time.time() - ts
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


@st.cache_data(show_spinner=False)
def _snap(db, stamp):
    return sc.load_snapshot(db)


# ------------------------------------------------------------------- scan
def _scan_symbols(universe):
    if st.session_state.get("scan_scope") == "codes":
        raw = st.session_state.get("scan_codes", "").replace("\n", ",").replace(";", ",").replace(" ", ",")
        syms = [data.norm(c) for c in raw.split(",") if c.strip()]
        return [s for s in dict.fromkeys(syms) if s != data.INDEX_SYM]
    return universe


def run_scan(db, demo, symbols, fresh_hours):
    with st.status("Scanning the market...", expanded=True) as status:
        bar = st.progress(0.0, text="Starting...")

        def cb(frac, msg):
            bar.progress(min(1.0, max(0.0, frac)), text=msg)

        stats = sc.download_universe(db, symbols, demo, progress=cb, fresh_hours=fresh_hours)
        bar.progress(1.0, text="Calculating indicators...")
        snap = sc.build_snapshot(db, progress=cb)
        st.session_state["scan_failed"] = stats["failed"]
        if snap.empty:
            status.update(label="No price data could be downloaded", state="error")
            st.error("Yahoo Finance returned no data. Check your connection (or try again in a few minutes).")
            return
        status.update(label=f"Done: {len(snap)} stocks analysed", state="complete", expanded=False)
    st.rerun()


# ------------------------------------------------------------------- page
def render(db, demo, names):
    universe = list(names.keys())
    info = sc.snapshot_info(db)
    st.subheader("Stock screener")
    st.caption("Find IDX stocks by technical conditions, including VWAP bands. "
               "Pick a preset, fine-tune the filters, then open any result in the chart.")

    # ---------- first run: nothing downloaded yet
    if not info["n"]:
        with st.container(border=True):
            est = max(1, round(len(universe) / 40 * 4 / 60))
            st.markdown("#### Step 0: download prices (one time)")
            st.write(f"The screener analyses about two years of daily prices for every stock. "
                     f"Downloading **{len(universe)} stocks** takes roughly **{est} min** the first time. "
                     "After that, *Update data* only fetches the new days.")
            if len(universe) < data.FULL_UNIVERSE_MIN and not demo:
                st.warning(f"Only {len(universe)} stocks are in the list (the starter list). "
                           "Open the sidebar → *Stock list* to load every IDX stock first, or scan this list now.")
            if st.button(f"Scan {len(universe)} stocks", type="primary"):
                run_scan(db, demo, universe, fresh_hours=6)
        return

    snap = _snap(db, info["at"])

    # ---------- data status bar
    c1, c2, c3 = st.columns([6, 1.6, 1.6], vertical_alignment="center")
    age_h = (time.time() - info["at"]) / 3600 if info["at"] else 999
    c1.caption(f"**{info['n']:,} stocks** analysed · latest prices **{info['last']}** · calculated {_ago(info['at'])}"
               + ("  ·  ⚠️ older than a day: press *Update data* to include the latest close" if age_h > 20 else ""))
    with c2:
        with st.popover("Scan options", **stretch(st.popover)):
            st.radio("Which stocks to update", ["all", "codes"], key="scan_scope",
                     format_func=lambda x: f"All {len(universe)} stocks in the list" if x == "all" else "Only the codes I type")
            if st.session_state["scan_scope"] == "codes":
                st.text_area("Codes (comma separated)", key="scan_codes", placeholder="BBCA, BBRI, TLKM")
    with c3:
        if st.button("Update data", type="primary", help="Download the newest prices and recalculate everything.",
                     **stretch(st.button)):
            run_scan(db, demo, _scan_symbols(universe), fresh_hours=3)
    failed = st.session_state.get("scan_failed") or []
    if failed:
        with st.expander(f"{len(failed)} stocks could not be downloaded (delisted, renamed or rate-limited)"):
            st.write(", ".join(data.label(s) for s in failed))
            st.caption("Press *Update data* again to retry. Stocks that keep failing are usually delisted.")

    # ---------- 1. presets
    st.markdown("##### 1 · Start from a preset")
    pnames = list(sc.PRESETS)
    for i in range(0, len(pnames), 5):
        cols = st.columns(5)
        for col, name in zip(cols, pnames[i:i + 5]):
            col.button(name, key=f"preset_{name}", help=sc.PRESETS[name][0], on_click=apply_preset, args=(name,),
                       type="primary" if st.session_state.get("preset") == name else "secondary", **stretch(st.button))
    cur = st.session_state.get("preset")
    if cur:
        st.info(sc.PRESETS[cur][0])

    # ---------- 2. filters
    with st.expander("2 · Fine-tune the filters", expanded=not cur):
        t_vwap, t_trend, t_mom, t_vol, t_base = st.tabs(["VWAP", "Trend", "Momentum", "Volume & range", "Basics"])
        with t_vwap:
            st.caption("VWAP is the volume-weighted average price since the start of the chosen period. "
                       "The ±1σ / ±2σ bands show how far price has stretched from it, measured in volume-weighted "
                       "standard deviations. Same calculation as on the chart.")
            a, b, c = st.columns(3)
            a.selectbox("VWAP restarts every", list(sc.PERIODS), format_func=sc.PERIODS.get, key="f_vwap_period",
                        help="Quarterly and yearly are the most common anchors.")
            b.selectbox("Price position", list(sc.VWAP_POS), format_func=sc.VWAP_POS.get, key="f_vwap_pos")
            if st.session_state["f_vwap_pos"].startswith("cross"):
                c.slider("Within the last N trading days", 1, 20, key="f_vwap_days")
        with t_trend:
            a, b, c = st.columns(3)
            a.selectbox("SMA 20", list(sc.SMA_OPTS), format_func=sc.SMA_OPTS.get, key="f_sma20")
            b.selectbox("SMA 50", list(sc.SMA_OPTS), format_func=sc.SMA_OPTS.get, key="f_sma50")
            c.selectbox("SMA 200", list(sc.SMA_OPTS), format_func=sc.SMA_OPTS.get, key="f_sma200")
            a, b, c = st.columns(3)
            a.selectbox("Moving-average order", list(sc.MA_STATE), format_func=sc.MA_STATE.get, key="f_ma_state")
            b.selectbox("Recent crossover", list(sc.MA_CROSS), format_func=sc.MA_CROSS.get, key="f_ma_cross")
            if st.session_state["f_ma_cross"] != "any":
                c.slider("Within the last N days", 1, 60, key="f_ma_cross_days")
        with t_mom:
            a, b = st.columns(2)
            a.slider("RSI (14)", 0, 100, key="f_rsi", help="Below 30 is often called oversold, above 70 overbought.")
            b.slider("1-month price change (%)", -50, 100, key="f_chg_1m",
                     help="Drag the ends inward to set a minimum / maximum. The outer ends mean 'no limit'.")
            a, b = st.columns(2)
            a.selectbox("MACD (12, 26, 9)", list(sc.MACD_OPTS), format_func=sc.MACD_OPTS.get, key="f_macd")
            if "cross" in st.session_state["f_macd"]:
                b.slider("Within the last N days", 1, 20, key="f_macd_days")
        with t_vol:
            a, b = st.columns(2)
            a.slider("Relative volume at least (× 20-day average)", 0.0, 5.0, step=0.1, key="f_rvol_min",
                     help="2.0 means today's volume is at least double the recent average.")
            b.slider("Within X% of the 52-week high (100 = no limit)", 5, 100, key="f_hi_within")
            a, b = st.columns(2)
            a.slider("New 52-week high within last N days (0 = off)", 0, 20, key="f_hi_days")
            b.checkbox("Bollinger squeeze (bands unusually narrow)", key="f_squeeze")
        with t_base:
            st.caption("Many IDX stocks trade very thinly. These defaults hide illiquid and inactive stocks.")
            a, b, c = st.columns(3)
            a.select_slider("Min. average traded value (Rp billion / day)", VALUE_OPTS, key="f_min_value_bn")
            b.number_input("Min. price (Rp)", min_value=0, step=10, key="f_min_price")
            c.slider("Hide stocks not traded for N days", 1, 30, key="f_active_days")
    st.button("Clear all filters", on_click=reset_filters, icon=":material/restart_alt:")

    # ---------- results
    filters = {k: st.session_state["f_" + k] for k in sc.FILTER_DEFAULTS}
    mask, parts = sc.apply_filters(snap, filters)
    res = snap[mask].sort_values("value20_bn", ascending=False)
    P = filters["vwap_period"]
    st.markdown(f"##### 3 · Results: {len(res)} of {len(snap)} stocks match")
    st.caption("Filters: " + "  ·  ".join(label for label, _ in parts))

    if res.empty:
        st.warning("No stock matches all the filters. Try loosening one of them.")
        counts = sorted(((int(m.sum()), label) for label, m in parts))
        st.caption("How many stocks pass each filter on its own (the smallest numbers are the strictest): "
                   + "; ".join(f"{label} → {n}" for n, label in counts[:4]))
        return

    show = pd.DataFrame({
        "Symbol": res["symbol"].str.replace(".JK", "", regex=False),
        "Name": res["name"],
        "Close": res["close"],
        "1D %": res["chg_1d"],
        "1M %": res["chg_1m"],
        "RVOL": res["rvol"],
        "RSI": res["rsi"],
        "vs SMA50 %": res["vs_sma50"],
        f"VWAP ({P})": res[f"vwap_{P}"],
        "vs VWAP %": res[f"vd_{P}"],
        "VWAP σ": res[f"z_{P}"],
        "From 52w high %": res["from_hi"],
        "Value Rp bn/d": res["value20_bn"],
    }).reset_index(drop=True)
    N = st.column_config.NumberColumn
    cfg = {
        "Close": N(format="%.0f"), "1D %": N(format="%.1f"), "1M %": N(format="%.1f"),
        "RVOL": N(format="%.1f", help="Volume today ÷ 20-day average volume"),
        "RSI": N(format="%.0f"), "vs SMA50 %": N(format="%.1f", help="How far price is above (+) or below (−) its 50-day average"),
        f"VWAP ({P})": N(format="%.0f", help=f"{sc.PERIODS[P]} anchored VWAP"),
        "vs VWAP %": N(format="%.1f", help="Distance of price from the VWAP"),
        "VWAP σ": N(format="%.2f", help="Price position in VWAP standard deviations: +1 = on the +1σ band, −2 = on the −2σ band"),
        "From 52w high %": N(format="%.1f", help="0 = at the 52-week high"),
        "Value Rp bn/d": N(format="%.1f", help="Average traded value per day over 20 days"),
    }
    event = st.dataframe(show, hide_index=True, column_config=cfg, on_select="rerun", selection_mode="single-row",
                         key="res_table", height=min(620, 36 * (len(show) + 1) + 3), **stretch(st.dataframe))
    sel = list(event.selection.rows) if event is not None and event.selection else []
    syms = list(res["symbol"])
    vw_used = P if filters["vwap_pos"] != "any" else None
    b1, b2, b3 = st.columns([2.4, 1.6, 6], vertical_alignment="center")
    if sel and sel[0] < len(syms):
        s = syms[sel[0]]
        b1.button(f"Open {data.label(s)} in chart", type="primary", on_click=goto_chart, args=(s, syms, vw_used),
                  **stretch(st.button))
    else:
        b1.button("Open first result in chart", type="primary", on_click=goto_chart, args=(syms[0], syms, vw_used),
                  **stretch(st.button))
    b2.download_button("Download CSV", show.to_csv(index=False).encode(), "idx_screener.csv", "text/csv",
                       **stretch(st.download_button))
    b3.caption("Tick the box at the left of a row to pick a stock. In the chart you can step through "
               "all results with the ◀ ▶ buttons.")
    st.caption("Indicators use daily Yahoo Finance data and can differ slightly from other platforms. "
               "A screen is a starting point for research, not a buy or sell signal.")
