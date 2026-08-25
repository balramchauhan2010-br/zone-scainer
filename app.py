# -*- coding: utf-8 -*-
"""
app.py — Institutional D&S Zone Scanner Dashboard (Streamlit)
================================================================
Timeframes  : 10m, 15m, 30m, 1h, 2h, 4h, 6h, Daily
Lookback    : 3 Months / 6 Months / 1 Year (हर TF की yfinance सीमा पर auto-clamp)
Zone Filter : Fresh ON/OFF, Tested ON/OFF
Universe    : सभी NSE स्टॉक्स + Global macro instruments
Chart Link  : Symbol पर क्लिक करते ही TradingView चार्ट (नया टैब)
Speed       : Batched yfinance download + session-level zone-cache
              (नया candle-close ना हो तो दोबारा scan नहीं होता)
"""

import concurrent.futures
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

from zone_core import scan_zones, latest_active_zones, DEFAULT_PARAMS

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


# ==========================================================================
# 1. CONSTANTS
# ==========================================================================
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

# हर टाइमफ्रेम की yfinance data-availability सीमा (दिनों में) — इससे ज़्यादा
# lookback मांगने पर स्वतः क्लैंप हो जाएगा (Yahoo Finance की hard limit है)।
TIMEFRAMES = {
    "10 Min":  {"yf_interval": "5m",  "resample": "10min",  "max_days": 58},
    "15 Min":  {"yf_interval": "5m",  "resample": "15min",  "max_days": 58},
    "30 Min":  {"yf_interval": "30m", "resample": None,     "max_days": 58},
    "1 Hour":  {"yf_interval": "60m", "resample": None,     "max_days": 725},
    "2 Hours": {"yf_interval": "60m", "resample": "120min", "max_days": 725},
    "4 Hours": {"yf_interval": "60m", "resample": "240min", "max_days": 725},
    "6 Hours": {"yf_interval": "60m", "resample": "360min", "max_days": 725},
    "Daily":   {"yf_interval": "1d",  "resample": None,     "max_days": None},  # No limit
}

LOOKBACK_OPTIONS = {"3 महीने": 90, "6 महीने": 182, "1 वर्ष": 365}

# --------------------- NSE STOCK UNIVERSE ---------------------
RAW_STOCKS = """TCS,M&M,HCLTECH,SBIN,INFY,HINDUNILVR,RELIANCE,BHARTIARTL,BEL,ONGC,
BAJAJ_AUTO,NESTLEIND,POWERGRID,ULTRACEMCO,ITC,ADANIPORTS,LT,COALINDIA,ADANIENT,
SUNPHARMA,MARUTI,ETERNAL,HDFCBANK,JSWSTEEL,NTPC,ASIANPAINT,DMART,KOTAKBANK,
TATASTEEL,TITAN,AXISBANK,SHRIRAMFIN,ICICIBANK,BAJFINANCE,MOTHERSON,
BRITANNIA,HEROMOTOCO,TVSMOTOR,PERSISTENT,TECHM,MCX,OIL,RECLTD,AUROPHARMA,COFORGE,
BSE,LAURUSLABS,EICHERMOT,LUPIN,CUMMINSIND,MUTHOOTFIN,INDUSTOWER,MAXHEALTH,
HINDALCO,JSWENERGY,BHARATFORG,WIPRO,HAVELLS,APLAPOLLO,TMPV,OBEROIRLTY,MARICO,
KEI,SBILIFE,DABUR,TATAPOWER,INDIGO,MFSL,DIXON,SBICARD,SRF,VBL,PFC,GODREJCP,
ASTRAL,UNITDSPR,GMRAIRPORT,IOC,HDFCAMC,TATACONSUM,HINDPETRO,LODHA,GRASIM,
TIINDIA,TORNTPHARM,UPL,HDFCLIFE,CANBK,SIEMENS,CGPOWER,APOLLOHOSP,VEDL,PNB,
FEDERALBNK,POLYCAB,PHOENIXLTD,AUBANK,INDUSINDBK,NAUKRI,ASHOKLEY,DIVISLAB,
NATIONALUM,DRREDDY,CIPLA,JINDALSTEL,POLICYBZR,AMBUJACEM,INDHOTEL,BPCL,
PIDILITIND,IDFCFIRSTB,ICICIGI,BANKBARODA,TMCV,JIOFIN,NMDC,CHOLAFIN,GAIL,TRENT"""

NSE_STOCKS = list(dict.fromkeys(
    [s.strip() for s in RAW_STOCKS.replace("\n", "").split(",") if s.strip()]
))

# yfinance / TradingView ticker fixes जहाँ नाम अलग है
YF_FIX = {"BAJAJ_AUTO": "BAJAJ-AUTO"}
TV_FIX = {"BAJAJ_AUTO": "BAJAJ-AUTO"}

def yf_ticker_for_stock(sym: str) -> str:
    return f"{YF_FIX.get(sym, sym)}.NS"

def tv_symbol_for_stock(sym: str) -> str:
    return f"NSE:{TV_FIX.get(sym, sym)}"

# --------------------- GLOBAL INSTRUMENTS ---------------------
# (label, yf_ticker_or_None, tv_symbol, chart_only)
# chart_only=True मतलब reliable free data-feed उपलब्ध नहीं — सिर्फ chart-link दिखेगा
GLOBAL_INSTRUMENTS = [
    ("DXY (US Dollar Index)",        "DX-Y.NYB",   "TVC:DXY",        False),
    ("USDINR",                       "INR=X",      "FX_IDC:USDINR",  False),
    ("TLT (20+Y US Treasury ETF)",   "TLT",        "NASDAQ:TLT",     False),
    ("US10Y (10-Yr Treasury Yield)", "^TNX",       "TVC:US10Y",      False),
    ("XAUUSD (Gold/USD)",            "GC=F",       "TVC:GOLD",       False),
    ("XAGUSD (Silver/USD)",          "SI=F",       "TVC:SILVER",     False),
    ("SPOTCRUDE (WTI Crude Oil)",    "CL=F",       "TVC:USOIL",      False),
    ("US30 (Dow Jones)",             "^DJI",       "TVC:DJI",        False),
    ("US500 (S&P 500)",              "^GSPC",      "TVC:SPX",        False),
    ("000001 (Shanghai Composite)",  "000001.SS",  "SSE:000001",     False),
    ("XIN9 (FTSE China A50)",        None,         "SGX:XIN9",       True),
    ("JP225 (Nikkei 225)",           "^N225",      "TVC:NI225",      False),
    ("GIFT NIFTY (NIFTY1!)",         None,         "NSEIX:NIFTY1!",  True),
    ("NIFTY 50 FUT (NIFTY1!)",       "^NSEI",      "NSE:NIFTY1!",    False),  # spot-proxy data
    ("FTSE100",                      "^FTSE",      "TVC:UKX",        False),
    ("DAX",                          "^GDAXI",     "TVC:DEU40",      False),
    ("ASX 200",                      "^AXJO",      "TVC:AS51",       False),
    ("CAC40",                        "^FCHI",      "TVC:CAC40",      False),
]

NSE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ==========================================================================
# 2. PAGE SETUP
# ==========================================================================
st.set_page_config(page_title="Institutional D&S Zone Scanner", layout="wide",
                    page_icon="📊", initial_sidebar_state="expanded")

st.markdown("""
<style>
[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
.zone-summary { background:#0B1F3A; color:white; padding:10px 16px; border-radius:10px;
    margin-bottom:12px; font-size:13px; display:flex; gap:18px; flex-wrap:wrap; }
</style>
""", unsafe_allow_html=True)

st.title("📊 Demand & Supply Zone Scanner")
st.caption("। DBR/RBR/RBD/DBD — सभी 4 पैटर्न कवर।")


def tv_link(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol)}"


# ==========================================================================
# 3. SIDEBAR CONTROLS
# ==========================================================================
st.sidebar.header("⚙️ Scan Settings")

scope = st.sidebar.multiselect("Universe चुनें", ["🇮🇳 NSE Stocks", "🌍 Global Instruments"],
                                default=["🇮🇳 NSE Stocks", "🌍 Global Instruments"])

selected_stocks = NSE_STOCKS
if "🇮🇳 NSE Stocks" in scope:
    with st.sidebar.expander("🇮🇳 NSE Stocks चुनें (डिफ़ॉल्ट: सभी)"):
        select_all_stocks = st.checkbox("सभी स्टॉक्स चुनें", value=True)
        if select_all_stocks:
            selected_stocks = NSE_STOCKS
        else:
            selected_stocks = st.multiselect("स्टॉक्स", NSE_STOCKS, default=NSE_STOCKS[:30])
else:
    selected_stocks = []

selected_globals = GLOBAL_INSTRUMENTS if "🌍 Global Instruments" in scope else []

st.sidebar.markdown("---")
tf_selected = st.sidebar.multiselect(
    "Timeframes चुनें", list(TIMEFRAMES.keys()),
    default=["15 Min", "1 Hour", "4 Hours", "Daily"]
)

lookback_label = st.sidebar.radio("Lookback Period (Current close candle से पीछे)",
                                   list(LOOKBACK_OPTIONS.keys()), index=0)
lookback_days = LOOKBACK_OPTIONS[lookback_label]

st.sidebar.markdown("---")
st.sidebar.subheader("🎯 Zone Filters")
show_fresh = st.sidebar.checkbox("🟢 Fresh Zones दिखाएं", value=True)
show_tested = st.sidebar.checkbox("🟡 Tested Zones दिखाएं", value=True)

category_filter = st.sidebar.selectbox("Pattern Category", ["सभी", "सिर्फ Reversal (DBR/RBD)", "सिर्फ Continuation (RBR/DBD)"])
hq_only = st.sidebar.checkbox("⭐ सिर्फ HQ Zones (Score ≥ 75)", value=False)

near_price_only = st.sidebar.checkbox("🎯 सिर्फ Near-Price Zones (pending order likely)", value=False)
proximity_pct = st.sidebar.slider("Near-Price Band (%)", 0.1, 5.0, 1.5, 0.1) if near_price_only else None

st.sidebar.markdown("---")
refresh_min = st.sidebar.slider("Auto-Refresh (मिनट, 0 = बंद)", 0.0, 15.0, 0.0, 0.5)
if HAS_AUTOREFRESH and refresh_min > 0:
    st_autorefresh(interval=int(refresh_min * 60 * 1000), key="auto_refresh")

if st.sidebar.button("🗑️ Cache साफ करें (Force पूरा Rescan)"):
    st.cache_data.clear()
    st.session_state.zone_cache = {}
    st.rerun()

scan_btn = st.sidebar.button("🔍 Scan Zones", type="primary", use_container_width=True)


# ==========================================================================
# 4. DATA FETCH (BATCHED + CACHED)
# ==========================================================================
def effective_period_days(tf_name: str, requested_days: int) -> int:
    max_days = TIMEFRAMES[tf_name]["max_days"]
    if max_days is None:
        return requested_days
    return min(requested_days, max_days)


@st.cache_data(ttl=300, show_spinner=False)
def batch_download(tickers_tuple: Tuple[str, ...], interval: str, days: int) -> Dict[str, pd.DataFrame]:
    if not tickers_tuple:
        return {}
    end = datetime.now(IST)
    start = end - timedelta(days=days + 2)
    try:
        data = yf.download(list(tickers_tuple), start=start, end=end, interval=interval,
                            group_by="ticker", progress=False, threads=True)
    except Exception:
        return {}
    out = {}
    for t in tickers_tuple:
        try:
            df = data[t].dropna() if len(tickers_tuple) > 1 else data.dropna()
            if not df.empty:
                out[t] = df
        except Exception:
            continue
    return out


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna()


def fetch_tf_data(tf_name: str, items: List[Tuple[str, str, str, bool]]) -> Dict[str, pd.DataFrame]:
    """items: [(display_name, yf_ticker_or_None, tv_symbol, chart_only), ...]"""
    cfg = TIMEFRAMES[tf_name]
    days = effective_period_days(tf_name, lookback_days)
    yf_tickers = tuple(sorted({it[1] for it in items if it[1] is not None}))
    raw = batch_download(yf_tickers, cfg["yf_interval"], days)

    out = {}
    for display_name, yf_t, tv_sym, chart_only in items:
        if yf_t is None or yf_t not in raw:
            continue
        df = raw[yf_t]
        if cfg["resample"]:
            df = resample_ohlcv(df, cfg["resample"])
        if len(df) >= 25:
            out[display_name] = df
    return out


def fetch_all_timeframes(tf_list: List[str], items) -> Dict[str, Dict[str, pd.DataFrame]]:
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tf_list), 8) or 1) as ex:
        futs = {ex.submit(fetch_tf_data, tf, items): tf for tf in tf_list}
        for fut in concurrent.futures.as_completed(futs):
            tf = futs[fut]
            try:
                results[tf] = fut.result()
            except Exception:
                results[tf] = {}
    return results


# ==========================================================================
# 5. SESSION-LEVEL ZONE CACHE (नया candle आने तक दोबारा scan नहीं)
# ==========================================================================
if "zone_cache" not in st.session_state:
    st.session_state.zone_cache = {}

def get_zones_cached(symbol: str, tf_name: str, lookback_label: str, df: pd.DataFrame):
    key = f"{symbol}::{tf_name}::{lookback_label}"
    last_ts = df.index[-1]
    cached = st.session_state.zone_cache.get(key)
    if cached is not None and cached["last_ts"] == last_ts and cached["n_bars"] == len(df):
        return cached["zones"]  # कोई नया candle नहीं -> तुरंत cache से लौटाओ

    lower_df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                   "Close": "close", "Volume": "volume"})
    zones = scan_zones(lower_df, params=DEFAULT_PARAMS)
    st.session_state.zone_cache[key] = {"zones": zones, "last_ts": last_ts, "n_bars": len(df)}
    return zones


# ==========================================================================
# 6. MAIN SCAN ORCHESTRATION
# ==========================================================================
def build_scan_items():
    items = []
    for s in selected_stocks:
        items.append((s, yf_ticker_for_stock(s), tv_symbol_for_stock(s), False))
    for label, yf_t, tv_sym, chart_only in selected_globals:
        items.append((label, yf_t, tv_sym, chart_only))
    return items


def category_matches(zone_category: str) -> bool:
    if category_filter == "सभी":
        return True
    if category_filter.startswith("सिर्फ Reversal"):
        return zone_category == "Reversal"
    return zone_category == "Continuation"


def run_full_scan():
    items = build_scan_items()
    item_map = {it[0]: it for it in items}  # display_name -> (name, yf, tv, chart_only)

    if not tf_selected or not items:
        st.warning("कृपया कम-से-कम एक Timeframe और Universe चुनें।")
        return None

    with st.spinner(f"⚡ {len(items)} instruments × {len(tf_selected)} timeframes स्कैन हो रहे हैं..."):
        all_tf_data = fetch_all_timeframes(tf_selected, items)

        rows = []
        total_fresh = total_tested = total_hq = 0

        for tf_name in tf_selected:
            tf_data = all_tf_data.get(tf_name, {})
            eff_days = effective_period_days(tf_name, lookback_days)

            for symbol, df in tf_data.items():
                zones = get_zones_cached(symbol, tf_name, lookback_label, df)
                if not zones:
                    continue

                allowed_states = set()
                if show_fresh: allowed_states.add("Fresh")
                if show_tested: allowed_states.add("Tested")
                if not allowed_states:
                    continue

                active = [z for z in zones if z.state in allowed_states]
                if hq_only:
                    active = [z for z in active if z.isHQ]
                active = [z for z in active if category_matches(z.zoneCategory)]

                if not active:
                    continue

                current_price = float(df["Close"].iloc[-1])
                _, yf_t, tv_sym, chart_only = item_map[symbol]

                for z in active:
                    if z.isDemand:
                        dist_pct = (current_price - z.proxVal) / z.proxVal * 100
                    else:
                        dist_pct = (z.proxVal - current_price) / z.proxVal * 100

                    if near_price_only and not (0 <= dist_pct <= proximity_pct):
                        continue

                    if z.state == "Fresh": total_fresh += 1
                    elif z.state == "Tested": total_tested += 1
                    if z.isHQ: total_hq += 1

                    direction_emoji = "🟢" if z.isDemand else "🔴"
                    state_tag = f"{z.state}" + (f" (#{z.touchCount})" if z.state == "Tested" else "")

                    rows.append({
                        "एसेट": symbol,
                        "Chart": tv_link(tv_sym),
                        "टाइमफ्रेम": tf_name,
                        "दिशा": f"{direction_emoji} {'DEMAND' if z.isDemand else 'SUPPLY'}",
                        "पैटर्न": z.patternType,
                        "टाइप": z.zoneCategory,
                        "स्टेट": state_tag,
                        "HQ": "⭐" if z.isHQ else "",
                        "स्कोर": z.densityScore,
                        "Entry": round(z.proxVal, 2),
                        "SL": round(z.slVal, 2),
                        "TP": round(z.tpVal, 2),
                        "LTP": round(current_price, 2),
                        "दूरी %": round(dist_pct, 2),
                        "Zone समय": z.timestamp.strftime("%d-%b %H:%M") if hasattr(z.timestamp, "strftime") else str(z.timestamp),
                        "_data_days": eff_days,
                    })

        return rows, total_fresh, total_tested, total_hq


# ==========================================================================
# 7. RENDER
# ==========================================================================
if scan_btn or st.session_state.get("has_scanned", False):
    st.session_state.has_scanned = True
    result = run_full_scan()

    if result:
        rows, total_fresh, total_tested, total_hq = result

        st.markdown(f"""
        <div class="zone-summary">
            <span>🕒 अंतिम स्कैन: {now_ist().strftime('%H:%M:%S')}</span>
            <span>📦 कुल Zones: {len(rows)}</span>
            <span>🟢 Fresh: {total_fresh}</span>
            <span>🟡 Tested: {total_tested}</span>
            <span>⭐ HQ: {total_hq}</span>
            <span>📅 Lookback: {lookback_label}</span>
        </div>
        """, unsafe_allow_html=True)

        if not rows:
            st.info("इन filters के साथ अभी कोई zone नहीं मिला। Filters ढीले करके देखें (Fresh/Tested दोनों ON करें, या HQ-only बंद करें)।")
        else:
            df_out = pd.DataFrame(rows)

            # डेटा-कवरेज नोट (intraday TF की Yahoo सीमा दिखाना ज़रूरी है)
            distinct_days = sorted(df_out["_data_days"].unique())
            for tf_name in tf_selected:
                cfg = TIMEFRAMES[tf_name]
                eff = effective_period_days(tf_name, lookback_days)
                if cfg["max_days"] is not None and eff < lookback_days:
                    st.caption(f"⚠️ **{tf_name}**: Yahoo Finance की सीमा के कारण असल में सिर्फ "
                               f"पिछले **{eff} दिन** का डेटा उपलब्ध है (आपने {lookback_days} दिन माँगे थे)।")

            df_out = df_out.drop(columns=["_data_days"])

            # sort: HQ पहले, फिर near-price पहले
            df_out["_hq_sort"] = df_out["HQ"].apply(lambda x: 0 if x == "⭐" else 1)
            df_out = df_out.sort_values(["_hq_sort", "दूरी %"]).drop(columns="_hq_sort")

            def highlight_row(row):
                if row["HQ"] == "⭐":
                    base = "background-color:#fff8e6;"
                elif "DEMAND" in row["दिशा"]:
                    base = "background-color:#eafbea;"
                else:
                    base = "background-color:#fdeeee;"
                return [base] * len(row)

            st.dataframe(
                df_out.style.apply(highlight_row, axis=1),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "एसेट": st.column_config.TextColumn("एसेट", width="small"),
                    "Chart": st.column_config.LinkColumn("📈 चार्ट खोलें", display_text="📈 Open"),
                },
                height=min(700, 60 + 35 * len(df_out)),
            )

            csv = df_out.to_csv(index=False).encode("utf-8-sig")
            st.download_button("⬇️ CSV डाउनलोड करें", csv, "zone_scan.csv", "text/csv")
else:
    st.info("👈 Sidebar से Timeframe/Universe/Lookback चुनकर **🔍 Scan Zones** बटन दबाएं।")
    st.markdown("""
    ### 📋 यह Scanner क्या करता है
    - **4 Institutional Patterns**: DBR (Demand-Reversal), RBR (Demand-Continuation),
      RBD (Supply-Reversal), DBD (Supply-Continuation)
    - **Leg-In pressure + CLV check**, **Base quality rules**, **Leg-Out explosive+volume-climax**,
      **Sweep+Rejection**, **Risk-normalization**, **Freshness decay** — सब शामिल
    - Symbol पर क्लिक करते ही TradingView चार्ट नए टैब में खुलेगा
    - एक बार scan होने के बाद, जब तक नया candle close नहीं होता, दोबारा compute नहीं होता (Fast ⚡)
    """)
