from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from data_service import download_batch, prepare_timeframe, scan_fingerprint
from universe import (GLOBAL_INSTRUMENTS, LOOKBACK_OPTIONS, NSE_STOCKS,
                      TIMEFRAMES, effective_days, stock_instrument, tv_link)
from zone_core import latest_active_zones, scan_zones, settings


st.set_page_config(page_title="Demand & Supply Scanner", page_icon="📊", layout="wide")
IST = ZoneInfo("Asia/Kolkata")
st.title("📊 Demand & Supply Zone Scanner")
st.caption("RBR • DBR • DBD • RBD | Structural rules only — कोई HQ/density scoring नहीं")


@st.cache_data(ttl=60, max_entries=32, show_spinner=False)
def cached_download(tickers: tuple[str, ...], interval: str, days: int):
    return download_batch(tickers, interval, days)


def cached_zones(ticker: str, tf: str, days: int, frame: pd.DataFrame, params: dict):
    cache = st.session_state.setdefault("zone_cache", {})
    key = (ticker, tf, days)
    fingerprint = scan_fingerprint(frame, params)
    previous = cache.get(key)
    if previous is not None and previous[0] == fingerprint:
        return previous[1]
    zones = scan_zones(frame, params=params)
    if len(cache) >= 1600 and key not in cache:
        cache.pop(next(iter(cache)))
    cache[key] = (fingerprint, zones)
    return zones


with st.sidebar:
    st.header("⚙️ Scan Settings")
    scope = st.multiselect("Universe", ["NSE watchlist", "Global instruments"],
                          default=["NSE watchlist", "Global instruments"])
    stocks = []
    if "NSE watchlist" in scope:
        with st.expander("NSE Stocks चुनें"):
            all_stocks = st.checkbox("पूरी दी हुई watchlist", value=True)
            stocks = NSE_STOCKS if all_stocks else st.multiselect(
                "Stocks", NSE_STOCKS, default=NSE_STOCKS[:10])
            custom = st.text_input("अतिरिक्त NSE symbols (comma-separated)",
                                   placeholder="HDFCBANK, ITC")
            stocks = list(dict.fromkeys(stocks + [s.strip().upper() for s in custom.split(",") if s.strip()]))
            st.caption("यह curated watchlist है; सभी NSE-listed securities की live सूची नहीं।")
    globals_selected = []
    if "Global instruments" in scope:
        names = [item.name for item in GLOBAL_INSTRUMENTS]
        chosen = st.multiselect("Global assets", names, default=names)
        globals_selected = [item for item in GLOBAL_INSTRUMENTS if item.name in chosen]
    timeframes = st.multiselect("Timeframes", list(TIMEFRAMES),
                               default=["15 Min", "1 Hour", "4 Hours", "Daily"])
    lookback_label = st.radio("Lookback", list(LOOKBACK_OPTIONS))
    days = LOOKBACK_OPTIONS[lookback_label]
    st.divider()
    st.subheader("Zone filters")
    show_fresh = st.checkbox("Fresh", value=True)
    show_tested = st.checkbox("Tested", value=True)
    category = st.selectbox("Category", ["सभी", "Reversal", "Continuation"])
    near_only = st.checkbox("केवल Near-Price zones", value=False)
    proximity = st.slider("Zone के nearest edge से दूरी (%)", 0.1, 5.0, 1.5, 0.1)
    st.caption("Zone के अंदर होने पर near-price दूरी 0% है।")
    st.divider()
    with st.expander("Capital / Risk / Target", expanded=False):
        capital = st.number_input("Account capital (₹)", min_value=1.0, value=25000.0, step=1000.0)
        risk_pct = st.number_input("Account risk / setup (%)", min_value=0.0, max_value=100.0,
                                   value=0.5, step=0.1)
        target_rr = st.number_input("Target R:R", min_value=0.1, value=5.0, step=0.5)
        sl_buffer = st.number_input("SL buffer × ATR", min_value=0.0, value=0.1, step=0.05)
        st.caption(f"Risk budget: ₹{capital * risk_pct / 100:,.2f}. यह position sizing या order placement नहीं है।")
    refresh = st.slider("Auto-refresh (मिनट; 0 = बंद)", 0.0, 15.0, 0.0, 0.5)
    st.caption("Downloaded data cache: 60 seconds. Yahoo data delayed हो सकता है।")
    force = st.button("🗑️ Download + zone cache साफ करें")
    scan = st.button("🔍 Scan Zones", type="primary", use_container_width=True)

if refresh > 0:
    st_autorefresh(interval=int(refresh * 60_000), key="zone_auto_refresh")
if force:
    cached_download.clear()
    st.session_state["zone_cache"] = {}
    st.rerun()
if scan:
    st.session_state["has_scanned"] = True

params = settings(accountCapital=capital, riskPct=risk_pct,
                  targetRR=target_rr, slBufferAtr=sl_buffer)
# BAJAJ_AUTO and BAJAJ-AUTO must not produce duplicate scan rows.
items = list({item.tv_symbol: item for item in
              [stock_instrument(s) for s in stocks] + globals_selected}.values())

with st.expander("डेटा और strategy के ज़रूरी नियम"):
    st.markdown("""
- **अंतिम उपलब्ध row से नया zone नहीं बनता**, चाहे वह candle बंद हो चुकी हो।
  लेकिन उसी row से existing zone Tested/Broken हो सकता है। यह मूल core का conservative नियम है।
- पहला proximal touch = Tested; अगली touching candle = Broken.
  लगातार दो touching candles भी दो touches हैं, अलग visits होना आवश्यक नहीं।
- Distal level का touch भी zone को Broken करता है; यह buffered SL execution नहीं है।
- NSE intraday aggregation **09:15 IST** से शुरू होता है। दिन का अंतिम छोटा bar रखा जाता है।
- Global derived bars UTC clock पर aggregate होते हैं; exchange sessions/lunch breaks/holidays
  का पूरा calendar लागू नहीं है। Native और TradingView bars अलग हो सकते हैं।
- Missing/zero leg-out volume पर volume check bypass होता है, जैसा मूल core में था।
- **Last price** अंतिम Yahoo candle का close है, live broker LTP नहीं। Zone समय leg-out का
  candle-label/start timestamp है, confirmation का wall-clock समय नहीं।
- Futures continuous series/rolls और Yahoo/TradingView feeds में अंतर हो सकता है।
- यह scanner है, trade backtest या guaranteed-return system नहीं; orders place नहीं करता।
""")

chart_only = [item for item in items if item.ticker is None]
if chart_only:
    st.subheader("Chart-only instruments")
    st.dataframe(pd.DataFrame([
        {"Asset": item.name, "Chart": tv_link(item.tv_symbol), "Status": "No scan feed configured"}
        for item in chart_only
    ]), hide_index=True, use_container_width=True,
        column_config={"Chart": st.column_config.LinkColumn("TradingView", display_text="चार्ट खोलें")})

if not st.session_state.get("has_scanned", False):
    st.info("Sidebar में चयन करके Scan Zones दबाएँ। बड़े universe के बजाय पहले कुछ stocks पर जाँचें।")
    st.stop()
if not timeframes or not items:
    st.warning("कम-से-कम एक timeframe और instrument चुनें।")
    st.stop()

feed_items = [item for item in items if item.ticker]
if not feed_items:
    st.info("चुने हुए instruments chart-only हैं; scan feed उपलब्ध नहीं है।")
    st.stop()

for tf in timeframes:
    effective = effective_days(tf, days)
    if effective < days:
        st.caption(f"⚠️ {tf}: {days} की जगह अधिकतम {effective} calendar days request होंगे। वास्तविक coverage नीचे देखें।")

# Group shared source intervals: 1h/2h/4h/6h reuse one 60m download.
requests = {}
for tf in timeframes:
    interval = TIMEFRAMES[tf][0]
    requests[interval] = max(requests.get(interval, 0), effective_days(tf, days))
tickers = tuple(sorted({item.ticker for item in feed_items}))
raw_by_interval, errors_by_interval = {}, {}
rows, diagnostics = [], []
allowed = ({"Fresh"} if show_fresh else set()) | ({"Tested"} if show_tested else set())
progress = st.progress(0.0, text="Data डाउनलोड हो रहा है…")

with st.spinner("OHLCV download और zone scan चल रहा है…"):
    for interval, requested_days in requests.items():
        raw_by_interval[interval], errors_by_interval[interval] = cached_download(
            tickers, interval, requested_days)
    completed, total = 0, len(timeframes) * len(feed_items)
    for tf in timeframes:
        interval = TIMEFRAMES[tf][0]
        for item in feed_items:
            diagnostic = {"Asset": item.name, "TF": tf, "Ticker": item.ticker,
                          "Requested days": effective_days(tf, days), "Bars": 0,
                          "First bar": "", "Last bar": "", "Status": "", "Feed note": item.note}
            raw = raw_by_interval[interval].get(item.ticker)
            try:
                if raw is None:
                    raise ValueError(errors_by_interval[interval].get(item.ticker, "No data returned"))
                frame = prepare_timeframe(raw, tf, item)
                diagnostic["Bars"] = len(frame)
                if not frame.empty:
                    diagnostic["First bar"] = str(frame.index[0])
                    diagnostic["Last bar"] = str(frame.index[-1])
                if len(frame) < max(25, params["atrPeriod"] + 5):
                    raise ValueError("Insufficient bars for this configuration")
                zones = cached_zones(item.ticker, tf, days, frame, params)
                active = latest_active_zones(zones)
                diagnostic["Status"] = f"Scanned: {len(zones)} total / {len(active)} active (before filters)"
                price = float(frame.close.iloc[-1])
                for zone in active:
                    if zone.state not in allowed or (category != "सभी" and zone.zoneCategory != category):
                        continue
                    lower, upper = sorted([zone.proxVal, zone.distVal])
                    gap = max(lower - price, price - upper, 0.0)
                    distance = gap / abs(zone.proxVal) * 100 if zone.proxVal else float("inf")
                    if near_only and distance > proximity:
                        continue
                    rows.append({
                        "Asset": item.name, "Chart": tv_link(item.tv_symbol, tf),
                        "TF": tf, "Direction": "DEMAND" if zone.isDemand else "SUPPLY",
                        "Pattern": zone.patternType, "Category": zone.zoneCategory,
                        "State": zone.state, "Touches": zone.touchCount,
                        "Entry": zone.proxVal, "SL": zone.slVal, "TP": zone.tpVal,
                        "Last price": price, "Zone distance %": distance,
                        "Entry-SL %": zone.riskPct,
                        "Zone time": str(zone.timestamp), "Price bar": str(frame.index[-1]),
                        "Feed note": item.note,
                    })
            except (ValueError, KeyError, TypeError) as exc:
                diagnostic["Status"] = f"Skipped: {exc}"
            diagnostics.append(diagnostic)
            completed += 1
            progress.progress(completed / total, text=f"{completed}/{total} — {item.name} · {tf}")
progress.empty()

st.caption(f"Scan completed: {datetime.now(IST):%d-%b-%Y %H:%M:%S %Z}")
a, b, c, d = st.columns(4)
a.metric("Visible zones", len(rows))
b.metric("Fresh", sum(row["State"] == "Fresh" for row in rows))
c.metric("Tested", sum(row["State"] == "Tested" for row in rows))
skipped = sum(row["Status"].startswith("Skipped:") for row in diagnostics)
d.metric("Skipped asset × TF", skipped)

if skipped:
    st.warning("कुछ scans data की कमी/error के कारण skip हुए। इन्हें 'कोई zone नहीं' न मानें; coverage देखें।")
if not rows:
    st.info("चुने हुए filters में कोई active zone नहीं मिला। Fresh/Tested filters और data coverage जाँचें।")
else:
    output = pd.DataFrame(rows).sort_values(["Zone distance %", "Asset", "TF"], kind="stable")
    config = {"Chart": st.column_config.LinkColumn("TradingView", display_text="📈 खोलें")}
    for name in ("Entry", "SL", "TP", "Last price"):
        config[name] = st.column_config.NumberColumn(name, format="%.4f")
    for name in ("Zone distance %", "Entry-SL %"):
        config[name] = st.column_config.NumberColumn(name, format="%.2f")
    st.dataframe(output, hide_index=True, use_container_width=True,
                 column_config=config, height=min(700, 70 + 35 * len(output)))
    st.download_button("⬇️ Zones CSV", output.to_csv(index=False).encode("utf-8-sig"),
                       "zone_scan.csv", "text/csv")

with st.expander("Data coverage / errors / provider notes", expanded=bool(skipped)):
    coverage = pd.DataFrame(diagnostics)
    st.dataframe(coverage, hide_index=True, use_container_width=True)
    st.download_button("⬇️ Coverage CSV", coverage.to_csv(index=False).encode("utf-8-sig"),
                       "data_coverage.csv", "text/csv")
st.caption("शैक्षिक उपयोग के लिए। Slippage, fees, lot sizes, margin, FX conversion और वास्तविक execution यहाँ model नहीं किए गए हैं।")
