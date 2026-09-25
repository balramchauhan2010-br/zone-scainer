# Demand & Supply Zone Scanner

Streamlit dashboard + pandas/NumPy zone engine. RBR, DBR, DBD और RBD patterns; scoring/HQ filtering नहीं है।

## फ़ाइलें

```text
zone-scanner/
├── app.py
├── zone_core.py
├── data_service.py
├── universe.py
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── .gitignore
├── .streamlit/
│   └── config.toml
├── .github/
│   └── workflows/
│       └── tests.yml
└── tests/
    └── test_scanner.py
```

`requirements.txt` में केवल package names/version ranges रखें; उसे किसी Python फ़ाइल के अंत में न चिपकाएँ। सभी चार `.py` files एक ही directory में रखें।

## Local run

Python 3.11+ इस्तेमाल करें।

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

फिर:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## GitHub → Streamlit Community Cloud

1. GitHub पर नया repository बनाएं। ऊपर दिखाई गई files/folders उसी structure में upload/commit करें।
2. `app.py`, `zone_core.py`, `data_service.py`, `universe.py` और `requirements.txt` repository root में रखें।
3. Streamlit Community Cloud में GitHub account connect करें और नया app बनाएँ।
4. Repository चुनें; branch वही चुनें जिसमें code है, आम तौर पर `main`।
5. **Main file path: `app.py`**। Advanced settings में उपलब्ध Python 3.11+ version चुनें; CI Python 3.11 के लिए configured है।
6. Deploy करें। इस scanner के लिए API key या Streamlit secrets आवश्यक नहीं हैं।
7. पहले 2–5 stocks और Daily/1 Hour पर scan जाँचें, फिर universe बढ़ाएँ।
8. Import/dependency error पर Cloud logs देखें। Yahoo throttling/blocked network/unsupported ticker मिलने पर app का coverage panel देखें।

यह code deployment के लिए व्यवस्थित है; आपके GitHub या Cloud account पर अपने-आप publish नहीं किया गया है। Yahoo connectivity और full-universe performance host/network पर निर्भर हैं।

## मूल code से सुधार

- `DEFAULT_PARAMS` export जोड़ा; पुराने imports compatible हैं। App validated `settings()` का उपयोग करता है।
- UI से HQ checkbox, score columns और HQ sorting हटाए; core में compatibility fields `densityScore=0`, `score10=0.0`, `isHQ=False` रहते हैं।
- OHLCV और numeric settings validation; unknown legacy config keys अब भी ignore होते हैं। गलत base counts अब silent clamp के बजाय error देते हैं: `1 <= min <= max <= 3`।
- Yahoo single-ticker और multi-ticker MultiIndex responses दोनों handle होते हैं। `auto_adjust=False` explicit है।
- Missing volume को zero माना जाता है। Non-finite/invalid OHLCV को silently repair करने के बजाय error मिलता है।
- NSE aggregation IST 09:15 से, हर date अलग; शुरुआती candle missing होने पर anchor नहीं खिसकता।
- `isOvernight` पूरा स्थानीय date compare करता है, weekday/timestamp unit पर निर्भर नहीं है।
- Supply box में `top >= bottom` रहता है; trade levels नहीं बदले।
- Cache key में पूरे OHLCV, timestamps और params का fingerprint है। एक ही timestamp की forming candle बदले तो states पुनः calculate होते हैं।
- साझा intervals reuse होते हैं: 1h/2h/4h/6h को एक 60m download मिलता है। Outer parallel Yahoo requests हटाए; 30 tickers/batch, 4 internal workers रखे।
- Download errors, insufficient bars और actual first/last timestamps अलग coverage table में दिखते हैं।
- Chart-only instruments सच में chart links के साथ दिखते हैं। Gold/Silver/WTI futures और NIFTY spot को spot/futures के गलत label से नहीं दिखाया जाता।
- Near-price filter nearest zone edge की absolute दूरी उपयोग करता है; zone के भीतर दूरी 0%।
- `realistic_roi()` placeholder है: अब `implemented=False` और return metrics `None` देता है, fake measured 0% return नहीं। बाहरी पुराने ROI callers को `implemented` check करना होगा।

## जानबूझकर बनाए रखे strategy rules

- Boring base body/TR ratio अधिकतम 0.55; base count 1–3।
- Strict TR hierarchy: base < leg-in < leg-out; strict visible-body hierarchy भी।
- Leg-in CLV/body requirements, opposite-cover rejection, full-engulf rejection, leg-out wick/explosion requirements और base-edge close confirmation।
- Leg-out volume missing/zero हो तो volume check bypass; अन्यथा volume > leg-in volume।
- एक bar पर अधिकतम एक zone; last 11 live zones पर duplicate check।
- Formation candle अपने zone को retest नहीं करती।
- पहला proximal-touch bar Tested; अगला touching bar Broken। लगातार touching bars भी अलग touches हैं।
- Distal touch सीधे Broken; buffered SL से यह अलग structural invalidation है।
- अंतिम उपलब्ध row से **नया zone कभी नहीं बनता**, even if historical/closed. उसी row से पहले के zones के states update होते हैं। Historical data पर भी last-row formation छूटेगी; यह original policy है, exchange-aware closed-candle detection नहीं।
- SL = distal ± ATR buffer; TP = proximal ± risk-per-unit × RR।
- `maxImbalanceMult` और `relaxGapCapOvernight` मूल code में effective gate नहीं थे; compatibility keys रखी हैं, gap-cap rule नया लागू नहीं किया। `testedLegOutRetracePct` metadata level ही है, retest state gate नहीं।

## डेटा सीमाएँ

- NSE list user-supplied curated watchlist है, complete/live NSE security master नहीं। Symbol changes validate करें या extra symbols box प्रयोग करें।
- Intraday lookback limits conservative request caps हैं; उपलब्ध history की guarantee नहीं। Missing source candles reconstruct नहीं होते।
- Day का अंतिम छोटा NSE bar retain होता है; हर 4h/6h labelled bar पूरे 4/6 घंटे का होना आवश्यक नहीं। Special sessions/holidays के लिए complete exchange calendar नहीं है।
- Derived global bars UTC clock पर aggregate होते हैं; local exchange opening/lunch calendar लागू नहीं। TradingView candles अलग हो सकती हैं।
- Daily bars provider timestamps रखते हैं; intraday global output UTC और NSE output IST होता है। Tables में timezone दिखाया जाता है।
- Price column Yahoo की last candle close है, broker live LTP नहीं। Download cache 60 seconds है; 30-second refresh भी नया feed सुनिश्चित नहीं करता।
- Cache-clear button cached downloads clear करता है; hosted app में यह cached function के shared entries भी clear कर सकता है। Zone cache session-local है।
- API keys नहीं चाहिए, लेकिन Yahoo availability, licensing/usage terms, throttling और corporate action/futures-roll effects का ध्यान रखें। `auto_adjust=False` होने से splits raw OHLC history में discontinuities ला सकते हैं।
- TradingView links external chart खोलते हैं; scanner के boxes अपने-आप TradingView में plot नहीं होते। Symbol availability वहाँ बदल सकती है।
- Capital/risk से account risk budget दिखता है; automatic position sizing, futures lot multipliers, FX conversion, margin, orders, fees/slippage या realised ROI model नहीं किए गए हैं।
- `backtest_summary()` केवल zone-state counts है, trade-performance backtest नहीं।

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q app.py zone_core.py data_service.py universe.py
python -m pytest -q
```

इस sandbox में **30 tests passed**: Python 3.13.14, Streamlit 1.64.0, pandas 2.2.3, NumPy 2.3.5, yfinance 0.2.66, streamlit-autorefresh 1.0.1, pytest 9.0.3। Tests offline/synthetic हैं और Streamlit initial page तथा mocked-data scan cover करते हैं। Live Yahoo feed, Streamlit Cloud deployment और financial performance का verification नहीं हुआ है। GitHub Actions workflow push/PR पर Python 3.11 tests चलाएगा।

**शैक्षिक उपयोग के लिए; यह निवेश सलाह या guaranteed profitable system नहीं है।**
