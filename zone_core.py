# -*- coding: utf-8 -*-
"""
app.py — Institutional D&S Zone Scanner (Streamlit UI)
========================================================================
zone_core.py (v9.0) ke saath 100% compatible.
Root-cause bug fix: run_full_scan() ab HAMESHA ek FLAT List[Zone]
return karta hai — chahe kitne bhi symbols scan ho rahe hon.
(Pehle wala bug: multi-symbol loop me `zones` list ke andar
 list-of-list ya tuple ghus gaya tha, isliye z.state fail hota tha.)
========================================================================
"""

import time
import numpy as np
import pandas as pd
import streamlit as st

from zone_core import (
    scan_zones,
    latest_active_zones,
    get_zone_alerts,
    diagnose_bar,
    DEFAULT_PARAMS,
    Zone,
)

# yfinance optional — agar aapke paas Dhan/kisi aur source ka data hai
# to fetch_data() function neeche replace kar sakte hain.
try:
    import yfinance as yf
    _HAS_YFINANCE = True
except Exception:
    _HAS_YFINANCE = False

st.set_page_config(page_title="Institutional D&S Zone Scanner", layout="wide")
st.title("🏛️ Institutional D&S Zone Scanner")

# ==============================================================================
# 1. SIDEBAR — Watchlist & Timeframe
# ==============================================================================
st.sidebar.header("⚙️ सेटिंग्स")

default_watchlist = "RELIANCE.NS, ONGC.NS, ICICIBANK.NS, BALRAMCHIN.NS, TCS.NS"
watchlist_text = st.sidebar.text_area(
    "Watchlist (comma-separated symbols)", value=default_watchlist, height=80
)
symbols = [s.strip().upper() for s in watchlist_text.split(",") if s.strip()]

interval = st.sidebar.selectbox(
    "Timeframe", ["5m", "15m", "30m", "1h", "1d"], index=1
)
period_map = {
    "5m": "60d", "15m": "60d", "30m": "60d", "1h": "180d", "1d": "2y"
}
period = period_map.get(interval, "60d")

st.sidebar.markdown("---")

# ==============================================================================
# 2. SIDEBAR — Advanced Zone Parameters (zone_core.DEFAULT_PARAMS se dynamic)
# ==============================================================================
with st.sidebar.expander("🔧 Advanced Zone Parameters", expanded=False):
    user_params = {}
    for key, default_val in DEFAULT_PARAMS.items():
        if isinstance(default_val, bool):
            user_params[key] = st.checkbox(key, value=default_val)
        elif isinstance(default_val, int):
            user_params[key] = st.number_input(key, value=int(default_val), step=1)
        elif isinstance(default_val, float):
            user_params[key] = st.number_input(key, value=float(default_val))
        else:
            user_params[key] = default_val

st.sidebar.markdown("---")

# ==============================================================================
# 3. SIDEBAR — Filters
# ==============================================================================
st.sidebar.subheader("🎯 Filters")

pattern_category = st.sidebar.selectbox(
    "Pattern Category", ["सभी", "Continuation", "Reversal"], index=0
)

only_hq = st.sidebar.checkbox("⭐ सिर्फ HQ Zones (Score ≥ 75)", value=False)
hq_min_score = 75

only_near_price = st.sidebar.checkbox(
    "🎯 सिर्फ Near-Price Zones (pending order likely)", value=False
)
near_price_pct = st.sidebar.slider(
    "Near-Price threshold (%)", min_value=0.5, max_value=10.0, value=3.0, step=0.5
)

st.sidebar.markdown("---")

# ==============================================================================
# 4. SIDEBAR — Auto Refresh & Cache
# ==============================================================================
auto_refresh_min = st.sidebar.slider(
    "Auto-Refresh (मिनट, 0 = बंद)", min_value=0.0, max_value=30.0, value=0.0, step=1.0
)

if st.sidebar.button("🗑️ Cache साफ करें (Force पूरा Rescan)"):
    st.cache_data.clear()
    st.sidebar.success("Cache साफ हो गया। अब 'Scan Zones' दबाएँ।")

scan_clicked = st.sidebar.button("🔍 Scan Zones", type="primary", use_container_width=True)

# ==============================================================================
# 5. DATA FETCH (yfinance) — cached
# ==============================================================================
@st.cache_data(ttl=300, show_spinner=False)
def fetch_data(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """
    Ek symbol ke liye OHLCV data laata hai.
    Agar aapke paas Dhan API hai to isi function ke andar
    apna data-fetch logic replace kar dein — bas return value
    columns ['open','high','low','close','volume'] + DatetimeIndex
    honi chahiye.
    """
    if not _HAS_YFINANCE:
        raise RuntimeError("yfinance install nahi hai. requirements.txt me add karein.")

    df = yf.download(
        symbol, period=period, interval=interval,
        progress=False, auto_adjust=False
    )
    if df is None or df.empty:
        return pd.DataFrame()

    # yfinance columns ko lowercase me normalize karo
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume"
    })
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    df.index = pd.to_datetime(df.index)
    return df


# ==============================================================================
# 6. FULL SCAN — ROOT-CAUSE FIX: hamesha FLAT List[Zone] return karo
# ==============================================================================
@st.cache_data(ttl=300, show_spinner=True)
def run_full_scan(symbols: list, interval: str, period: str, params: dict):
    """
    Multi-symbol scan.
    ✅ FIX: 'zones' hamesha ek SEEDHI/FLAT list rahegi jisme sirf
             Zone dataclass objects hon — koi nested list/tuple nahi.
    Har Zone object me hum 'symbol' attribute manually attach karte
    hain (dataclass frozen nahi hai, isliye setattr allowed hai).
    """
    all_zones: list = []          # <-- yehi woh list hai jo pehle corrupt ho rahi thi
    errors: dict = {}

    for sym in symbols:
        try:
            df = fetch_data(sym, interval, period)
            if df is None or df.empty or len(df) < 20:
                errors[sym] = "डेटा उपलब्ध नहीं / बहुत कम bars"
                continue

            symbol_zones = scan_zones(df, params=params)   # -> List[Zone]

            # ✅ FIX: .append() nahi, .extend() use karo taaki
            #         list-of-lists ki jagah ek hi flat list bane
            for z in symbol_zones:
                setattr(z, "symbol", sym)          # symbol tag laga do
                setattr(z, "last_close", float(df["close"].iloc[-1]))
            all_zones.extend(symbol_zones)          # <-- root-cause fix

        except Exception as e:
            errors[sym] = str(e)

    return all_zones, errors     # <-- caller ko explicitly dono milte hain


# ==============================================================================
# 7. AUTO-REFRESH LOGIC (simple, external package ke bina)
# ==============================================================================
if auto_refresh_min > 0:
    st.sidebar.caption(f"⏱️ हर {auto_refresh_min:.0f} मिनट में auto-refresh होगा।")
    # session_state me last refresh time track karo
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = time.time()

    elapsed_min = (time.time() - st.session_state.last_refresh) / 60.0
    if elapsed_min >= auto_refresh_min:
        st.session_state.last_refresh = time.time()
        st.cache_data.clear()
        st.rerun()

# ==============================================================================
# 8. RUN SCAN
# ==============================================================================
if "zones_result" not in st.session_state:
    st.session_state.zones_result = None
    st.session_state.errors_result = None

if scan_clicked or (auto_refresh_min > 0 and st.session_state.zones_result is None):
    zones, errors = run_full_scan(symbols, interval, period, user_params)
    st.session_state.zones_result = zones
    st.session_state.errors_result = errors

zones = st.session_state.zones_result
errors = st.session_state.errors_result

# ==============================================================================
# 9. RESULTS DISPLAY
# ==============================================================================
if zones is None:
    st.info("👈 Sidebar se settings चुनकर **'Scan Zones'** बटन दबाएँ।")
    st.stop()

if errors:
    with st.expander(f"⚠️ {len(errors)} symbol(s) me समस्या आई", expanded=False):
        for sym, msg in errors.items():
            st.write(f"**{sym}**: {msg}")

if len(zones) == 0:
    st.warning("कोई zone नहीं मिला। Parameters relax करें या अलग symbols try करें।")
    st.stop()

# ---------------- FILTERS APPLY (safe, .state hamesha available hoga) ----------------
# ✅ यहाँ अब कभी AttributeError नहीं आएगा क्योंकि zones हमेशा List[Zone] है
active_states = {"Fresh", "Tested"}
filtered = [z for z in zones if z.state in active_states]

if pattern_category != "सभी":
    filtered = [z for z in filtered if z.zoneCategory == pattern_category]

if only_hq:
    filtered = [z for z in filtered if z.densityScore >= hq_min_score]

if only_near_price:
    near_list = []
    for z in filtered:
        lc = getattr(z, "last_close", None)
        if lc is None or z.proxVal <= 0:
            continue
        if z.isDemand:
            diff_pct = abs((lc - z.proxVal) / z.proxVal) * 100
        else:
            diff_pct = abs((z.proxVal - lc) / z.proxVal) * 100
        if diff_pct <= near_price_pct:
            near_list.append(z)
    filtered = near_list

st.success(f"✅ कुल {len(zones)} zones मिले | Filter के बाद: {len(filtered)}")

# ---------------- TABLE बनाना ----------------
rows = []
for z in filtered:
    lc = getattr(z, "last_close", np.nan)
    sym = getattr(z, "symbol", "-")
    if z.proxVal > 0 and not np.isnan(lc):
        if z.isDemand:
            dist_pct = ((lc - z.proxVal) / z.proxVal) * 100
        else:
            dist_pct = ((z.proxVal - lc) / z.proxVal) * 100
    else:
        dist_pct = np.nan

    rows.append({
        "Symbol": sym,
        "Type": "🟢 Demand" if z.isDemand else "🔴 Supply",
        "Pattern": z.patternType,
        "Category": z.zoneCategory,
        "State": z.state,
        "HQ": "⭐" if z.isHQ else "",
        "Score": z.densityScore,
        "Entry (Prox)": round(z.proxVal, 2),
        "SL": round(z.slVal, 2),
        "TP": round(z.tpVal, 2),
        "LTP": round(lc, 2) if not np.isnan(lc) else None,
        "Dist %": round(dist_pct, 2) if not np.isnan(dist_pct) else None,
        "Touches": z.touchCount,
        "Overnight Gap": "✅" if z.isOvernightGap else "",
        "Created At": z.timestamp,
    })

result_df = pd.DataFrame(rows)
if not result_df.empty:
    result_df = result_df.sort_values(
        by=["HQ", "Dist %"], ascending=[False, True], na_position="last"
    )

st.subheader("📋 Zone Results")
st.dataframe(result_df, use_container_width=True, hide_index=True)

# ---------------- Download ----------------
if not result_df.empty:
    csv = result_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ CSV Download करें", data=csv, file_name="zones.csv", mime="text/csv")
