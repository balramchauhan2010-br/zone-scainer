"""Curated watchlist, not the complete/current NSE security master."""
from dataclasses import dataclass
from urllib.parse import urlencode

TIMEFRAMES = {
    "10 Min": ("5m", 10, 58),
    "15 Min": ("5m", 15, 58),
    "30 Min": ("30m", 30, 58),
    "1 Hour": ("60m", 60, 725),
    "2 Hours": ("60m", 120, 725),
    "4 Hours": ("60m", 240, 725),
    "6 Hours": ("60m", 360, 725),
    "Daily": ("1d", None, None),
}
LOOKBACK_OPTIONS = {"3 महीने": 90, "6 महीने": 182, "1 वर्ष": 365}
RAW_STOCKS = """TCS,M&M,HCLTECH,SBIN,INFY,HINDUNILVR,RELIANCE,BHARTIARTL,BEL,ONGC,
BAJAJ_AUTO,NESTLEIND,POWERGRID,ULTRACEMCO,ITC,ADANIPORTS,LT,COALINDIA,ADANIENT,SUNPHARMA,
MARUTI,ETERNAL,HDFCBANK,JSWSTEEL,NTPC,ASIANPAINT,DMART,KOTAKBANK,TATASTEEL,TITAN,
AXISBANK,SHRIRAMFIN,ICICIBANK,BAJFINANCE,MOTHERSON,BRITANNIA,HEROMOTOCO,TVSMOTOR,PERSISTENT,TECHM,
MCX,OIL,RECLTD,AUROPHARMA,COFORGE,BSE,EICHERMOT,LUPIN,CUMMINSIND,MUTHOOTFIN,
INDUSTOWER,MAXHEALTH,HINDALCO,JSWENERGY,BHARATFORG,WIPRO,HAVELLS,APLAPOLLO,TMPV,OBEROIRLTY,
MARICO,SBILIFE,DABUR,TATAPOWER,INDIGO,MFSL,DIXON,SBICARD,SRF,VBL,
PFC,GODREJCP,ASTRAL,UNITDSPR,GMRAIRPORT,IOC,HDFCAMC,TATACONSUM,HINDPETRO,LODHA,
GRASIM,TIINDIA,TORNTPHARM,UPL,HDFCLIFE,CANBK,SIEMENS,CGPOWER,APOLLOHOSP,VEDL,
PNB,POLYCAB,PHOENIXLTD,AUBANK,INDUSINDBK,NAUKRI,ASHOKLEY,DIVISLAB,DRREDDY,CIPLA,
JINDALSTEL,POLICYBZR,AMBUJACEM,INDHOTEL,BPCL,PIDILITIND,IDFCFIRSTB,ICICIGI,BANKBARODA,TMCV,
JIOFIN,NMDC,CHOLAFIN,GAIL,TRENT,DLF,BHEL,HAL,IRCTC,ALKEM,
ABB,ABCAPITAL,BALKRISIND,BOSCHLTD,GODREJPROP,COLPAL,CONCOR,LICI"""
NSE_STOCKS = list(dict.fromkeys(s.strip() for s in RAW_STOCKS.split(",") if s.strip()))


@dataclass(frozen=True)
class Instrument:
    name: str
    ticker: str | None
    tv_symbol: str
    nse: bool = False
    note: str = ""


def stock_instrument(symbol: str) -> Instrument:
    symbol = symbol.strip().upper().removesuffix(".NS")
    symbol = {"BAJAJ_AUTO": "BAJAJ-AUTO"}.get(symbol, symbol)
    return Instrument(symbol, f"{symbol}.NS", f"NSE:{symbol}", True)


GLOBAL_INSTRUMENTS = [
    Instrument("DXY", "DX-Y.NYB", "TVC:DXY"),
    Instrument("USDINR", "INR=X", "FX_IDC:USDINR"),
    Instrument("TLT", "TLT", "NASDAQ:TLT"),
    Instrument("US10Y", "^TNX", "TVC:US10Y", note="Yahoo ^TNX quote scale; verify against chart"),
    Instrument("Gold futures", "GC=F", "COMEX:GC1!", note="Gold futures, not XAUUSD spot"),
    Instrument("Silver futures", "SI=F", "COMEX:SI1!", note="Silver futures, not XAGUSD spot"),
    Instrument("WTI futures", "CL=F", "NYMEX:CL1!", note="WTI futures, not spot crude"),
    Instrument("Dow Jones", "^DJI", "TVC:DJI"),
    Instrument("S&P 500", "^GSPC", "TVC:SPX"),
    Instrument("Shanghai Composite", "000001.SS", "SSE:000001"),
    Instrument("China A50 — chart only", None, "SGX:XIN9"),
    Instrument("Nikkei 225", "^N225", "TVC:NI225"),
    Instrument("GIFT NIFTY — chart only", None, "NSEIX:NIFTY1!"),
    Instrument("NIFTY 50 spot", "^NSEI", "NSE:NIFTY", True, "Spot index, not NIFTY futures"),
    Instrument("FTSE 100", "^FTSE", "TVC:UKX"),
    Instrument("DAX", "^GDAXI", "TVC:DEU40"),
    Instrument("ASX 200", "^AXJO", "TVC:AS51"),
    Instrument("CAC 40", "^FCHI", "TVC:CAC40"),
]


def tv_link(symbol: str, tf: str | None = None) -> str:
    query = {"symbol": symbol}
    if tf:
        minutes = TIMEFRAMES[tf][1]
        query["interval"] = str(minutes) if minutes else "D"
    return "https://www.tradingview.com/chart/?" + urlencode(query)


def effective_days(tf: str, requested: int) -> int:
    limit = TIMEFRAMES[tf][2]
    return min(requested, limit) if limit else requested
