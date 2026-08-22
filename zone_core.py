# -*- coding: utf-8 -*-
# फाइल की एन्कोडिंग UTF-8 सेट की गई है ताकि हिंदी व अन्य कैरेक्टर्स सही से प्रोसेस हों

"""
zone_core.py — v8.3 (Advanced D&S Engine with Leg-In Body-Strength Rule)
यह एक एडवांस्ड डिमांड और सप्लाई (D&S) ज़ोन डिटेक्शन इंजन है।

v8.2 से v8.3 में क्या बदला (जैसा आपने कहा)
--------------------------------------------------------------------------
  1-4) पुराने syntax bugs (`>` की जगह `=`, dict की missing closing bracket,
       undefined swingLeftBars/swingRightBars) — यह सब पहले जैसा ही ठीक
       रखा गया है (कोई बदलाव नहीं)।

  5) `sweptLiquidity` field: पहले इसे जोड़ा गया था, अब पूरी तरह से
     HATA DIYA GAYA है — Zone dataclass से, Zone बनाते समय, और
     get_zone_alerts() के output dict से भी। यह feature इस version में
     इस्तेमाल ही नहीं होता था (हमेशा False set होता था), इसलिए बेकार का
     field था।

  6) FIX (इस पास में): DEFAULT_PARAMS में दो एंट्री (`hqLegOuttrMult` और
     `hqLegIntrMult`) invalid Python syntax में लिखी गई थीं
     (जैसे `hqLegOuttrMult>legintr ×2.0` — यह dict value नहीं बन सकता)।
     इन्हें ठीक करके सही keys/values बना दिया गया है:
         hqLegOutTrMult = 2.0   -> Leg-Out TR, Leg-In TR का कम से कम 2.0x हो
         hqLegInAtrMult = 1.5   -> Leg-In TR, ATR का कम से कम 1.5x हो
     साथ ही density-score सेक्शन में इन्हीं keys का सही नाम से (और सही
     `tr(t, legInIdx)` फ़ंक्शन-कॉल सिंटैक्स से, ना कि गलत `tr[t - leginIdx]`
     array-indexing से) इस्तेमाल किया गया है — पहले वहाँ `leginIdx` नाम का
     undefined वेरिएबल भी था (सही नाम `legInIdx` है), वह भी ठीक किया गया।

  7) FIX (इस पास में): density score कैलकुलेशन के बाद बीच में एक हिंदी
     वाक्य ("और 60 अंक से कम वाले zone को वैलिड नहीं माने") सीधे कोड के
     बीच में लिखा हुआ था, जो valid Python नहीं है। आपके कहे मुताबिक इसका
     मतलब लागू किया गया है: अब densityScore < 60 वाला कोई भी zone सिरे से
     invalid मानकर discard (skip) कर दिया जाता है — सिर्फ >=70 वाला HQ
     होता है, 60-69 वाला normal (non-HQ) valid zone होता है, और <60 वाला
     zone बनता ही नहीं। यह चेक ज़ोन बनने और duplicate-filter से पहले किया
     जाता है ताकि किसी भी अमान्य ज़ोन के कारण बाद में (उसी candle पर)
     बड़ी baseCount आज़माने का मौका ना छूटे।

  NEW RULE — Leg-In कैंडल की "Body Strength" चेक:
  --------------------------------------------------------------------------
  आपका कहना है: DBR (Drop-Base-Rally, Demand) में leg-in वाली गिरने वाली
  (bearish) candle में SELLING PRESSURE ज़्यादा दिखनी चाहिए, और RBD
  (Rally-Base-Drop, Supply) में leg-in वाली चढ़ने वाली (bullish) candle
  में BUYING PRESSURE ज़्यादा दिखनी चाहिए। इसका मतलब है:
      - candle की BODY बड़ी हो (Open-Close के बीच का फ़ासला ज़्यादा हो)
      - ऊपर और नीचे दोनों तरफ़ की WICK (shadow) छोटी हो

  इसे लागू करने के लिए एक नया चेक जोड़ा गया है:
      bodyPct = |Close - Open| / (High - Low)
      bodyPct >= legInMinBodyPct (default 0.60, यानी body कम से कम 60%)

  यह check दोनों तरफ़ (Demand aur Supply, DBR/RBR/DBD/RBD सभी pattern
  types) के leg-in candle par apply hota hai, kyunki "saaf, dabaav wali"
  candle हर pattern में ज़रूरी है — chahe woh selling pressure ho ya
  buying pressure.

------------------------------------------------------------------
FULL VALIDATION (v8.3)
------------------------------------------------------------------
  LEG-IN:
    - correct direction (bull/bear)
    - Body Strength: |Close-Open| / (High-Low) >= 60%   [NEW]
    - Opposite-color पीछे वाली candle 50%+ cover ना करे  [v8.2 से]
    - TR >= 1.0 x ATR
    - TR >= 2.0 x Max Base TR

  BASE (1-3 candles):
    - each candle TR <= 1.0 x ATR

  LEG-OUT:
    - correct direction
    - Explosive: TR >= 1.2 x leg in TR
    - Wick % <= 25%
    - TR Hierarchy: LegOut > LegIn > MaxBaseTR
    - Volume: Volume[legOut] > Volume[legIn]
    - Imbalance (if useImbalance):
        Demand: Low > MaxBaseHigh  OR  Close > LegInHigh
        Supply: High < MinBaseLow  OR  Close < LegInLow
        + gap का size legIn TR से बड़ा नहीं हो                [v8.2 से]

  SCORE:
    - densityScore < 60  -> zone सिरे से invalid (discard)      [v8.3 fix]
    - densityScore >= 70 -> High-Quality (HQ) zone

Public entry points:
    scan_zones(df, params=None, lookback_months=None) -> List[Zone]
    latest_active_zones(zones, ...)                    -> List[Zone]
    get_zone_alerts(zones, current_price, ..)          -> List[dict]
"""

from dataclasses import dataclass  # डेटा स्ट्रक्चर को आसानी से क्लास के रूप में डिफाइन करने के लिए dataclass मॉड्यूल
from typing import List, Optional, Dict, Any  # टाइप हिंटिंग (Type Hints) के लिए आवश्यक डेटा टाइप्स
import numpy as np  # तेज़ एरे ऑपरेशन्स और गणितीय गणना के लिए NumPy
import pandas as pd  # टाइम-सीरीज़ और टेक्निकल डेटा प्रोसेसिंग के लिए Pandas


# सिस्टम के डिफ़ॉल्ट पैरामीटर्स की डिक्शनरी (मशीन/अल्गो की मूल सेटिंग्स)
DEFAULT_PARAMS = dict(
    # --- कैपिटल और रिस्क सेटिंग्स ---
    accountCapital=25000.0,   # खाता की कुल पूँजी ($25,000)
    riskPct=0.5,              # प्रति ट्रेड लिया जाने वाला रिस्क प्रतिशत (0.5%)
    targetRR=5.0,             # रिस्क-टू-रिवॉर्ड अनुपात लक्ष्य (1:5)
    slBufferAtr=0.1,          # स्टॉपलॉस में ATR का अतिरिक्त बफर (0.1x ATR)

    # --- एल्गोरिदम और फिल्टर्स ---
    atrPeriod=14,             # ATR (Average True Range) इंडिकेटर की अवधि (14 कैंडल्स)
    volSmaPeriod=20,          # औसतन वॉल्यूम निकालने की अवधि (20 कैंडल्स का SMA)
    legOutTrMult=1.2,         # Leg-Out कैंडल का न्यूनतम True Range मल्टीप्लायर (1.2x ATR)
    hqLegOutTrMult=2.0,       # FIX: हाई-क्वालिटी Leg-Out कैंडल का TR मल्टीप्लायर (Leg-In TR का 2.0x)
    hqLegInAtrMult=1.5,       # FIX: हाई-क्वालिटी Leg-In कैंडल का TR (ATR का 1.5x)
    maxBaseAtrMult=1.0,       # बेस कैंडल का अधिकतम आकार (1.0x ATR से बड़ा न हो)
    maxWickPct=0.25,          # Leg-Out कैंडल में अधिकतम विक/शैडो % (25%)

    # --- बेस और लेग-इन नियम ---
    minBaseCount=1,             # ज़ोन में कम से कम बेस कैंडल्स (1)
    maxBaseCount=3,             # ज़ोन में अधिकतम बेस कैंडल्स (3)
    legInMinAtrMult=1.0,        # Leg-In कैंडल का TR कम से कम 1.0x ATR होना चाहिए (valid syntax में ठीक किया गया)
    minClvPct=0.60,             # Leg-In कैंडल का न्यूनतम Close Location Value (60%)
    legInToBaseSizeMult=2.0,    # Leg-In कैंडल सबसे बड़ी बेस कैंडल से कम से कम 2x बड़ी होनी चाहिए (valid syntax में ठीक किया गया)
    legInMinBodyPct=0.60,       # NEW: Leg-In कैंडल की BODY, उसकी कुल रेंज (High-Low) का कम से कम 60% होनी चाहिए
                                 #      (यानी ऊपर+नीचे मिलाकर wick ज़्यादा से ज़्यादा 40% ही हो) -> साफ़ selling/buying pressure

    # --- इमबैलेंस सेटिंग्स ---
    useImbalance=True,             # प्राइस इमबैलेंस (Gap/Fast Move) चेक करें
    maxImbalanceVsLegInMult=1.0,   # gap/imbalance का साइज़ leg-in TR से बड़ा नहीं होना चाहिए (1.0x)

    # --- विपरीत-रंग वाली पीछे की candle का filter ---
    rejectOppositeCoverPct=0.50,   # agar leg-in ke theek peeche wali opposite-color
                                    # candle leg-in ki range ka >=50% cover kare to zone invalid

    # --- डेंसिटी स्कोर थ्रेशोल्ड ---
    minValidScore=60,              # NEW (v8.3 fix): इससे कम स्कोर वाला zone सिरे से invalid माना जाता है
    hqScoreThreshold=70,           # इतने या इससे ज़्यादा स्कोर वाला zone High-Quality (HQ) माना जाता है
)

_HARD_MAX_BASE_COUNT = 3  # कोड में बेस कैंडल की अधिकतम सीमा 3 पर लॉक की गई है


# ज़ोन के सभी प्रॉपर्टीज को स्टोर करने के लिए ज़ोन डेटा क्लास
@dataclass
class Zone:
    proxVal: float                  # प्रॉक्सिमल लाइन (एंट्री प्राइस स्तर)
    distVal: float                  # डिस्टल लाइन (ज़ोन का बाहरी किनारा)
    slVal: float                    # स्टॉपलॉस स्तर (Stop Loss)
    tpVal: float                    # टारगेट स्तर (Take Profit)
    isDemand: bool                  # True = Demand Zone, False = Supply Zone
    isHQ: bool                      # हाई-क्वालिटी ज़ोन का फ्लैग (True/False)
    densityScore: int               # ज़ोन का क्वालिटी स्कोर (0 से 100)
    patternType: str = ""           # पैटर्न प्रकार: RBR, DBR, DBD, RBD
    zoneCategory: str = ""          # ज़ोन प्रकार: Continuation या Reversal
    state: str = "Fresh"            # स्थिति: Fresh (अन-टच), Tested (Leg out candle 50%cover,टच हुआ), Broken (base कैंडल wick या बॉडी tuch को टूटा हुआ)
    touchCount: int = 0             # प्राइस द्वारा ज़ोन को टेस्ट करने की संख्या
    originalDensityScore: int = 0   # ज़ोन निर्माण के समय का शुरुआती स्कोर
    startBarIndex: int = 0          # ज़ोन शुरू होने वाली कैंडल का इंडेक्स
    createdBarIndex: int = 0        # ज़ोन पूरा बनने वाली कैंडल (Leg-Out) का इंडेक्स
    baseCount: int = 0              # ज़ोन में शामिल बेस कैंडल्स की संख्या
    timestamp: object = None        # ज़ोन बनने की तिथि व समय
    qty: float = 0.0                # रिस्क मैनेजमेंट के आधार पर ट्रेड क्वांटिटी
    # NOTE: sweptLiquidity field जानबूझकर यहाँ से HATA DI GAYI है (v8.3), जैसा माँगा गया


# Wilder's Smoothing विधि द्वारा ATR (Average True Range) की गणना करने वाला फ़ंक्शन
def _wilder_atr(high, low, close, period):
    n = len(high)  # कैंडल्स की कुल संख्या
    tr = np.empty(n)  # True Range के लिए खाली NumPy सरणी (Array)
    tr[0] = high[0] - low[0]  # पहली कैंडल का TR (High - Low)
    if n > 1:
        prev_close = close[:-1]  # पिछली कैंडल का Close प्राइस
        # True Range की गणना: Max(H-L, |H-PrevClose|, |L-PrevClose|)
        tr[1:] = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
        )
    atr = np.full(n, np.nan)  # ATR रिजल्ट के लिए NaN से भरा एरे
    if n >= period:
        seed = tr[:period].mean()  # शुरुआती 14 कैंडल्स का सिंपल मीन (Seed ATR)
        atr[period - 1] = seed  # Seed वैल्यू असाइन की गई
        if n > period:
            alpha = 1.0 / period  # वाइल्डर स्मूथिंग फैक्टर (1/14)
            tail = pd.Series(tr[period:])  # बाकी बचा हुआ TR डेटा
            seeded = pd.concat([pd.Series([seed]), tail], ignore_index=True)  # डेटा को Seed के साथ जोड़ना
            smoothed = seeded.ewm(alpha=alpha, adjust=False).mean().to_numpy()  # Exponential Smoothing लागू की
            atr[period:] = smoothed[1:]  # स्मूथेड ATR परिणाम स्टोर किया गया
    return atr  # अंतिम ATR एरे रिटर्न किया गया


# लुकबैक महीनों (Lookback Months) के हिसाब से स्कैनिंग शुरू करने का इंडेक्स तय करना
def _resolve_start_bar_for_lookback(df: pd.DataFrame, lookback_months: Optional[float]) -> int:
    n = len(df)  # डेटाफ्रेम की कुल लंबाई
    if lookback_months is None or lookback_months <= 0 or n == 0:
        return 0  # अगर लुकबैक सेट नहीं है तो शुरुआत (0) से ही स्कैन करें
    idx = df.index  # डेटाफ्रेम का टाइमस्टैम्प इंडेक्स
    if isinstance(idx, pd.DatetimeIndex):
        cutoff = idx[-1] - pd.DateOffset(months=lookback_months)  # कट-ऑफ दिनांक
        pos = idx.searchsorted(cutoff, side="left")  # उस तारीख का कैंडल इंडेक्स ढूंढा
        return int(max(0, pos))  # सुरक्षित इंडेक्स रिटर्न किया
    approx_bars = int(round(lookback_months * 21))  # औसतन 21 ट्रेडिंग दिवस/महीना मानकर कैंडल्स निकालीं
    return int(max(0, n - approx_bars))  # निकाला गया कैंडल इंडेक्स रिटर्न किया


# --------------------------------------------------------------------------
# मुख्य स्कैनिंग इंजन (Core Scan Function) - ज़ोन ढूंढने का मुख्य फ़ंक्शन
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
               lookback_months: Optional[float] = None) -> List[Zone]:
    p = dict(DEFAULT_PARAMS)  # डिफ़ॉल्ट सेटिंग्स लोड की गईं (एक कॉपी बनाई ताकि मूल dict ना बदले)
    if params:
        p.update(params)  # यूजर द्वारा दी गई कस्टम सेटिंग्स से अपडेट किया गया

    # बेस कैंडल काउंट की सीमा चेक व सेट की गई (कभी भी hard-limit से ज़्यादा नहीं)
    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    # तीव्र गणितीय गणना के लिए Pandas सिरीज़ को NumPy एरे में बदला गया (numpy, pandas से बहुत तेज़ चलता है)
    o = df["open"].to_numpy(dtype=float)    # Open कीमतें
    h = df["high"].to_numpy(dtype=float)    # High कीमतें
    l = df["low"].to_numpy(dtype=float)     # Low कीमतें
    c = df["close"].to_numpy(dtype=float)   # Close कीमतें
    v = df["volume"].to_numpy(dtype=float)  # Volume डेटा
    n = len(df)  # कैंडल की कुल संख्या

    minBaseCount = p["minBaseCount"]  # न्यूनतम बेस काउंट (लोकल वेरिएबल में कॉपी, बार-बार dict lookup से बचने के लिए)
    maxBaseCount = p["maxBaseCount"]  # अधिकतम बेस काउंट
    atrPeriod = p["atrPeriod"]        # ATR पीरियड

    # ATR और 20-पीरियड मूविंग एवरेज वॉल्यूम की गणना (एक बार पूरी सीरीज़ के लिए, बार-बार नहीं)
    atr = _wilder_atr(h, l, c, atrPeriod)
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()

    # सापेक्ष इंडेक्स पर True Range (TR) निकालने के लिए इंटरनल फ़ंक्शन
    # t = वर्तमान कैंडल का absolute index, idx = t से कितनी कैंडल पीछे (0 = खुद t)
    def tr(t, idx):
        return h[t - idx] - l[t - idx]

    # बुलिश (ग्रीन) कैंडल जाँचने का फ़ंक्शन
    def is_bull(t, idx):
        return c[t - idx] > o[t - idx]

    # बेरिश (रेड) कैंडल जाँचने का फ़ंक्शन
    def is_bear(t, idx):
        return o[t - idx] > c[t - idx]

    # कैंडल में विक्स (Wicks) का अनुपात निकालने का फ़ंक्शन (ऊपर+नीचे wick / कुल रेंज)
    def wick_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]  # कैंडल की कुल लंबाई (High - Low)
        if rng == 0:
            return 0.0
        wicks = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])  # ऊपर व नीचे की विक का योग
        return wicks / rng  # कुल लंबाई के मुकाबले विक का %

    # NEW: कैंडल की BODY का % निकालने का फ़ंक्शन (body / कुल रेंज)
    # यह wick_pct का उल्टा concept है: bodyPct + wickPct हमेशा 1.0 (100%) के बराबर होगा
    def body_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]  # कैंडल की कुल रेंज
        if rng == 0:
            return 0.0
        body = abs(c[i] - o[i])  # Open और Close के बीच का absolute फ़ासला (candle body)
        return body / rng  # कुल रेंज के मुकाबले body का %

    zones: List[Zone] = []  # पाए गए सभी ज़ोन्स की सूची (मास्टर लिस्ट, कभी shrink नहीं होती)
    active_zones: List[Zone] = []  # वर्तमान में क्रियाशील (Non-Broken) ज़ोन्स (state-tracking के लिए अलग रखी)

    # स्कैनिंग शुरू करने का सबसे पहला सुरक्षित कैंडल इंडेक्स
    # (+3 इसलिए ताकि legInIdx+1 वाली "leg-in के पीछे की candle" भी हमेशा उपलब्ध रहे)
    min_start = max(atrPeriod, maxBaseCount + 3, 11)
    record_from_bar = max(min_start, _resolve_start_bar_for_lookback(df, lookback_months))  # स्कैनिंग रिकॉर्ड इंडेक्स

    # Leg-Out मल्टीप्लायर वैल्यू प्राप्त करना (backward-compatible: legOutAtrMult भी accept करता है)
    legOutMult = p.get("legOutTrMult", p.get("legOutAtrMult", 1.2))

    # पूरे प्राइस डेटा पर कैंडल-बाय-कैंडल लूप (हर candle को संभावित "leg-out" मानकर टेस्ट किया जाता है)
    for t in range(min_start, n):
        if np.isnan(atr[t]):  # अगर ATR उपलब्ध न हो (warm-up period) तो आगे बढ़ें
            continue

        zoneFoundOnThisBar = False  # इस कैंडल पर ज़ोन मिलने का ट्रैकिंग फ्लैग

        # 1 से 3 बेस कैंडल्स के लिए प्रयास करना (छोटी बेस काउंट पहले try होती है)
        for baseCount in range(minBaseCount, maxBaseCount + 1):
            if zoneFoundOnThisBar:  # यदि छोटी बेस काउंट में ज़ोन मिल गया हो तो लूप रोकें
                break

            legOutIdx = 0            # Leg-Out वर्तमान कैंडल t पर है (सबसे हाल की कैंडल)
            legInIdx = baseCount + 1 # Leg-In कैंडल बेस कैंडल्स से ठीक पहले की कैंडल है
            prevIdx = legInIdx + 1   # Leg-In के ठीक पीछे (और पुरानी) वाली candle (opposite-cover rule के लिए)

            if t - prevIdx < 0 or t - baseCount < 0:
                continue  # डेटा अपर्याप्त होने पर छोड़ें (शुरुआती bars पर पीछे इतना data नहीं होगा)
            if np.isnan(atr[t - legInIdx]) or np.isnan(atr[t]):
                continue  # ATR अमान्य होने पर छोड़ें

            # ---------------- LEG-IN की जाँच ----------------
            legInTR = tr(t, legInIdx)      # Leg-In का TR (True Range)
            legInLow = l[t - legInIdx]     # Leg-In का Low
            legInHigh = h[t - legInIdx]    # Leg-In का High
            legInClose = c[t - legInIdx]   # Leg-In का Close
            legInVol = v[t - legInIdx]     # Leg-In वॉल्यूम
            legInRng = legInHigh - legInLow  # Leg-In की कुल रेंज (High - Low)

            legInIsBull = is_bull(t, legInIdx)  # क्या Leg-In ग्रीन है (bullish)
            legInIsBear = is_bear(t, legInIdx)  # क्या Leg-In रेड है (bearish)

            if legInRng == 0:  # 0 रेंज वाली (यानी High==Low) अमान्य कैंडल, आगे ना बढ़ें
                continue

            # ---------------- NEW RULE: Leg-In की BODY STRENGTH चेक ----------------
            # माँग: DBR में selling pressure ज़्यादा (बड़ी लाल body, छोटी wicks),
            #       RBD में buying pressure ज़्यादा (बड़ी हरी body, छोटी wicks)।
            # इसे लागू करने का तरीका: candle की body, उसकी कुल range का कम से कम
            # legInMinBodyPct (डिफ़ॉल्ट 60%) होनी चाहिए। यह दिशा (bull/bear) दोनों
            # पर बराबर लागू होता है, क्योंकि "साफ़, दबाव वाली" candle हर pattern
            # (DBR हो या RBD) में ज़रूरी है।
            legInBodyPct = body_pct(t, legInIdx)
            if legInBodyPct < p["legInMinBodyPct"]:
                continue  # body बहुत छोटी / wicks बहुत बड़ी -> साफ़ दबाव नहीं दिखा -> reject

            # ---------------- Opposite-color पीछे वाली candle का 50%+ cover चेक ----------------
            # Agar leg-in ke theek peeche (prevIdx) wali candle opposite रंग की है aur woh
            # leg-in ki range ka >=50% (rejectOppositeCoverPct) hissa overlap karti hai,
            # to yeh zone invalid maani jayegi (choppy/indecisive area, saaf directional
            # leg-in nahi).
            prevIsBull = is_bull(t, prevIdx)
            prevIsBear = is_bear(t, prevIdx)
            isOppositeColor = (legInIsBull and prevIsBear) or (legInIsBear and prevIsBull)
            if isOppositeColor:
                prevHigh = h[t - prevIdx]
                prevLow = l[t - prevIdx]
                overlap = max(0.0, min(prevHigh, legInHigh) - max(prevLow, legInLow))
                coverPct = overlap / legInRng
                if coverPct >= p["rejectOppositeCoverPct"]:
                    continue  # opposite-color candle ne leg-in ko 50%+ cover kiya -> invalid

            # Close Location Value (CLV) निकालना — pattern classification में इस्तेमाल होगी
            bullClv = (legInClose - legInLow) / legInRng  # बुलिश CLV स्कोर (close ऊपर के कितने पास बंद हुआ)
            bearClv = (legInHigh - legInClose) / legInRng # बेरिश CLV स्कोर (close नीचे के कितने पास बंद हुआ)

            # ---------------- BASE की जाँच ----------------
            allBaseValid = True        # सभी बेस कैंडल्स वैध हैं या नहीं
            maxBaseTR = 0.0            # बेस कैंडल्स में सबसे बड़ा TR
            maxBaseHigh = -1.0         # बेस कैंडल्स में सबसे अधिक High
            minBaseLow = float("inf")  # बेस कैंडल्स में सबसे कम Low
            hasOppositeColorBase = False  # क्या विपरीत रंग की बेस कैंडल है (density score के लिए)

            # सभी बेस कैंडल्स पर लूप चलाकर आकार व High/Low निकालना
            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    allBaseValid = False
                    break
                bTR = tr(t, b)  # संबंधित बेस कैंडल का TR

                # नियम: बेस कैंडल की TR, ATR से छोटी होनी चाहिए (consolidation area)
                if bTR > (p["maxBaseAtrMult"] * atr[t - b]):
                    allBaseValid = False
                    break

                if bTR > maxBaseTR:
                    maxBaseTR = bTR  # सबसे बड़ा Base TR स्टोर किया

                if h[t - b] > maxBaseHigh:
                    maxBaseHigh = h[t - b]  # उच्चतम High अपडेट किया
                if l[t - b] < minBaseLow:
                    minBaseLow = l[t - b]   # न्यूनतम Low अपडेट किया

            if not allBaseValid or maxBaseTR == 0:
                continue  # बेस कैंडल अमान्य होने पर आगे बढ़ें

            # नियम: Leg-In कैंडल सबसे बड़ी बेस कैंडल से कम से कम 2 गुना बड़ी होनी चाहिए
            if legInTR < (p["legInToBaseSizeMult"] * maxBaseTR):
                continue

            # Leg-In का न्यूनतम आकार 1x ATR से बड़ा होना चाहिए
            validLegIn = legInTR >= (p["legInMinAtrMult"] * atr[t - legInIdx])
            if not validLegIn:
                continue

            # ---------------- LEG-OUT की जाँच ----------------
            legOutTR = tr(t, legOutIdx)      # Leg-Out का TR
            legOutHigh = h[t - legOutIdx]    # Leg-Out का High
            legOutLow = l[t - legOutIdx]     # Leg-Out का Low
            legOutClose = c[t - legOutIdx]   # Leg-Out का Close
            legOutVol = v[t - legOutIdx]     # Leg-Out का वॉल्यूम

            isDemandLegOut = is_bull(t, legOutIdx)  # क्या Leg-Out बुलिश है (Demand)
            isSupplyLegOut = is_bear(t, legOutIdx)  # क्या Leg-Out बेरिश है (Supply)
            if not (isDemandLegOut or isSupplyLegOut):
                continue  # अनिश्चित (doji जैसी) कैंडल होने पर छोड़ें

            # Leg-Out विस्फोटक (Explosive) व वैध होनी चाहिए
            isLegOutExplosive = legOutTR >= (legOutMult * atr[t - legOutIdx])  # कम से कम 1.2x ATR
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["maxWickPct"]        # बत्तियाँ <= 25%
            passesTRHierarchy = (legOutTR > legInTR) and (legInTR > maxBaseTR)   # आकार अनुक्रम: LegOut > LegIn > Base
            passesVolume = legOutVol > legInVol                                  # LegOut का वॉल्यूम LegIn से अधिक हो

            # ---------------- प्राइस इमबैलेंस चेकिंग (gap-size limit सहित) ----------------
            hasImbalance = True
            if p["useImbalance"]:
                if isDemandLegOut:
                    # डिमांड: Leg-Out Low बेस High से ऊपर हो या Close Leg-In High से ऊपर हो
                    gapCond = (legOutLow > maxBaseHigh) or (legOutClose > legInHigh)
                    # gap का असली साइज़ leg-in TR से बड़ा नहीं होना चाहिए
                    gapSize = max(0.0, legOutLow - maxBaseHigh)
                    hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
                elif isSupplyLegOut:
                    # सप्लाई: Leg-Out High बेस Low से नीचे हो या Close Leg-In Low से नीचे हो
                    gapCond = (legOutHigh < minBaseLow) or (legOutClose < legInLow)
                    # gap का असली साइज़ leg-in TR से बड़ा नहीं होना चाहिए
                    gapSize = max(0.0, minBaseLow - legOutHigh)
                    hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)

            # ---------------- पैटर्न वर्गीकरण (Pattern Classification) ----------------
            isRBR = legInIsBull and (bullClv >= p["minClvPct"]) and isDemandLegOut  # Rally-Base-Rally
            isDBR = legInIsBear and (bearClv >= p["minClvPct"]) and isDemandLegOut  # Drop-Base-Rally
            isDBD = legInIsBear and (bearClv >= p["minClvPct"]) and isSupplyLegOut  # Drop-Base-Drop
            isRBD = legInIsBull and (bullClv >= p["minClvPct"]) and isSupplyLegOut  # Rally-Base-Drop

            # सभी शर्तों का एक साथ वैलिडेशन (सब true होने पर ही zone मान्य होगा)
            isValid = (
                (isRBR or isDBR or isDBD or isRBD)
                and isLegOutExplosive
                and isLegOutWickValid
                and passesTRHierarchy
                and passesVolume
                and hasImbalance
            )

            if not isValid:
                continue  # कोई शर्त पूरी न होने पर छोड़ें

            # ---------------- डेंसिटी स्कोर (Density Score Calculation) ----------------
            densityScore = 0  # स्कोर 0 से शुरू

            # 1. ज़ोन में केवल 1 बेस कैंडल होने पर (+15 अंक) — जितनी छोटी base, उतनी अच्छी
            if baseCount == 1:
                densityScore += 15

            # 2. Leg-In कैंडल >= 1.5x ATR होने पर (+10 अंक)
            if legInTR >= (p["hqLegInAtrMult"] * atr[t - legInIdx]):
                densityScore += 10

            # 3. Leg-Out कैंडल >= leg-in candle का 2.0x TR होने पर (+15 अंक)
            #    FIX: पहले यहाँ गलत key `hqLegintrMult`, undefined वेरिएबल `leginIdx`,
            #    और invalid `tr[t - leginIdx]` array-indexing सिंटैक्स था। अब सही key
            #    `hqLegOutTrMult` और सही function-call `tr(t, legInIdx)` इस्तेमाल किया गया है।
            if legOutTR >= (p["hqLegOutTrMult"] * tr(t, legInIdx)):
                densityScore += 15

            # 4. Leg-In >= 2x Base और Leg-Out >= 2x Leg-In का अनुपात होने पर (+15 अंक)
            if (legInTR >= 2.0 * maxBaseTR) and (legOutTR >= 2.0 * legInTR):
                densityScore += 15

            # 5. Leg-Out का वॉल्यूम 20-SMA से ज्यादा होने पर (+10 अंक)
            if legOutVol > vol_sma[t - legOutIdx]:
                densityScore += 10

            # 6. Leg-Out का क्लोज़ शरीर के ऊपरी/निचले 80% भाग में होने पर (+15 अंक)
            if isDemandLegOut:
                legOutBodyPos = (legOutClose - legOutLow) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.80:
                    densityScore += 15
            else:
                legOutBodyPos = (legOutHigh - legOutClose) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.80:
                    densityScore += 15

            # 7. डिमांड में Red Base या सप्लाई में Green Base कैंडल होने पर (+10 अंक)
            for b in range(1, baseCount + 1):
                if isDemandLegOut and is_bear(t, b):
                    hasOppositeColorBase = True
                    break
                elif isSupplyLegOut and is_bull(t, b):
                    hasOppositeColorBase = True
                    break
            if hasOppositeColorBase:
                densityScore += 10

            # 8. फ्रेश ज़ोन (Fresh Zone) होने पर बोनस (+10 अंक)
            densityScore += 10

            # ---------------- FIX (v8.3): स्कोर-आधारित वैलिडिटी फ़िल्टर ----------------
            # आपके कहे मुताबिक: 60 से कम स्कोर वाले zone को वैलिड नहीं माना जाएगा,
            # यानी वो zone सिरे से discard हो जाएगा (ना तो normal ना ही HQ zone बनेगा)।
            # zoneFoundOnThisBar अभी True नहीं किया गया, ताकि यह ज़ोन reject होने पर
            # इसी candle t पर बड़ी baseCount भी आज़माई जा सके।
            if densityScore < p["minValidScore"]:
                continue  # स्कोर बहुत कम -> zone invalid -> अगली baseCount try करें

            # 70 या उससे अधिक अंक पाने वाले ज़ोन को High Quality (HQ) माना जाएगा
            isHQZone = densityScore >= p["hqScoreThreshold"]

            zoneFoundOnThisBar = True  # ज़ोन सफलतापूर्वक खोज लिया गया (score-filter भी पास हुआ)

            # ---------------- प्रॉक्सिमल और डिस्टल लाइन्स (Entry/SL/TP/Qty) ----------------
            # प्रॉक्सिमल (एंट्री स्तर): डिमांड में Base का High, सप्लाई में Base का Low
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            # डिस्टल (ज़ोन की सीमा): डिमांड में Base का Low, सप्लाई में Base का High
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            # स्टॉपलॉस: डिस्टल लाइन + ATR बफर
            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)  # प्रति शेयर रिस्क
            # टेक प्रॉफिट: प्रॉक्सिमल + (रिस्क * R:R)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            # रिस्क-आधारित क्वांटिटी कैलकुलेशन
            riskAmount = p["accountCapital"] * (p["riskPct"] / 100.0)  # खाते का कुल डॉलर रिस्क
            qty = round(riskAmount / riskPerShare, 2) if riskPerShare > 0 else 0.0  # शेयर्स/लॉट संख्या

            # ---------------- डुप्लीकेट ज़ोन फिल्टर ----------------
            isDuplicate = False
            checked = 0
            # पिछले 11 ज़ोन से तुलना करके अति-निकटतम ज़ोन को हटाया जाता है
            for checkZ in reversed(zones):
                if checkZ.state == "Broken":
                    continue  # टूट चुके पुराने ज़ोन से तुलना ना करें (वरना नया valid zone गलती से discard हो सकता है)
                # यदि एक ही प्रकार का ज़ोन 0.25x ATR के दायरे में पहले से मौजूद है
                if checkZ.isDemand == isDemandLegOut and abs(checkZ.proxVal - proxVal) < (atr[t] * 0.25):
                    isDuplicate = True
                    break
                checked += 1
                if checked >= 11:
                    break
            if isDuplicate:
                continue  # डुप्लीकेट ज़ोन को स्किप करें

            # पैटर्न प्रकार और कैटेगरी असाइन की गई
            if isRBR:
                patternType, zoneCategory = "RBR", "Continuation"
            elif isDBR:
                patternType, zoneCategory = "DBR", "Reversal"
            elif isDBD:
                patternType, zoneCategory = "DBD", "Continuation"
            else:
                patternType, zoneCategory = "RBD", "Reversal"

            leftBar = t - baseCount  # ज़ोन की शुरुआत का इंडेक्स

            # नया Zone ऑब्जेक्ट निर्मित किया गया (sweptLiquidity अब पास नहीं किया जाता, field हटा दी गई है)
            newZone = Zone(
                proxVal=proxVal, distVal=distVal, slVal=slVal, tpVal=tpVal,
                isDemand=isDemandLegOut, isHQ=isHQZone, densityScore=densityScore,
                patternType=patternType, zoneCategory=zoneCategory, state="Fresh",
                touchCount=0, originalDensityScore=densityScore,
                startBarIndex=leftBar, createdBarIndex=t, baseCount=baseCount,
                timestamp=df.index[t], qty=qty,
            )
            zones.append(newZone)        # मास्टर लिस्ट में जोड़ा गया
            active_zones.append(newZone) # एक्टिव ज़ोन लिस्ट में जोड़ा गया

        # ---------------- ज़ोन स्टेटस ट्रैकिंग (Fresh, Tested, Broken) ----------------
        # यह हर candle t पर चलता है, ताकि पुराने zones का state (टच/टूट) अपडेट होता रहे
        if active_zones:
            lo_t, hi_t = l[t], h[t]  # वर्तमान कैंडल का Low और High
            still_active = []        # जीवित (Non-Broken) ज़ोन्स की सूची
            for z in active_zones:
                if z.state == "Fresh":
                    if z.isDemand:
                        # अगर मूल्य प्रॉक्सिमल से नीचे आए लेकिन डिस्टल न तोड़े
                        if lo_t <= z.proxVal and lo_t > z.distVal:
                            z.state = "Tested"  # ज़ोन पहली बार टेस्ट हुआ
                            z.touchCount += 1
                        elif lo_t <= z.distVal:
                            z.state = "Broken"  # डिस्टल लाइन टूटी, ज़ोन अमान्य
                    else:
                        # सप्लाई ज़ोन टेस्ट या ब्रेक
                        if hi_t >= z.proxVal and hi_t < z.distVal:
                            z.state = "Tested"  # ज़ोन टेस्ट हुआ
                            z.touchCount += 1
                        elif hi_t >= z.distVal:
                            z.state = "Broken"  # ज़ोन टूटा
                elif z.state == "Tested":
                    if z.isDemand:
                        if lo_t <= z.distVal:
                            z.state = "Broken"  # डिमांड ज़ोन टूटा
                        elif lo_t <= z.proxVal:
                            z.touchCount += 1   # दोबारा टच हुआ
                    else:
                        if hi_t >= z.distVal:
                            z.state = "Broken"  # सप्लाई ज़ोन टूटा
                        elif hi_t >= z.proxVal:
                            z.touchCount += 1   # दोबारा टच हुआ

                if z.state != "Broken":
                    still_active.append(z)  # केवल एक्टिव ज़ोन्स को ही आगे रखा
            active_zones = still_active     # एक्टिव लिस्ट अपडेट की गई

    if lookback_months is None:
        return zones  # अगर कोई लुकबैक फ़िल्टर नहीं है तो सभी ज़ोन लौटाएँ
    # केवल तय लुकबैक अवधि के भीतर निर्मित ज़ोन रिटर्न करें
    return [z for z in zones if z.createdBarIndex >= record_from_bar]


# केवल एक्टिव (Fresh या Tested) ज़ोन निकालने के लिए हेल्पर फ़ंक्शन
def latest_active_zones(zones: List[Zone], include_tested: bool = True) -> List[Zone]:
    states = {"Fresh"} | ({"Tested"} if include_tested else set())  # एक्टिव राज्यों का सेट
    return [z for z in zones if z.state in states]  # फ़िल्टर्ड सूची रिटर्न की गई


# वर्तमान प्राइस के पास मौजूद ज़ोन के लिए लाइव अलर्ट जनरेट करने वाला फ़ंक्शन
def get_zone_alerts(zones, current_price, min_proximity_pct=0.0, max_proximity_pct=1.0,
                     include_tested=True) -> List[Dict[str, Any]]:
    alerts = []  # अलर्ट्स की लिस्ट
    candidates = latest_active_zones(zones, include_tested=include_tested)  # एक्टिव ज़ोन चुने
    for z in candidates:
        if z.proxVal <= 0:
            continue
        if z.isDemand:
            diff_pct = (current_price - z.proxVal) / z.proxVal  # डिमांड ज़ोन से वर्तमान प्राइस का अंतर %
            direction = "DEMAND"
        else:
            diff_pct = (z.proxVal - current_price) / z.proxVal  # सप्लाई ज़ोन से अंतर %
            direction = "SUPPLY"
        # तय की गई निकटता (proximity percentage) सीमा की जाँच
        if not (min_proximity_pct <= diff_pct <= max_proximity_pct):
            continue
        # अलर्ट डिक्शनरी बनाकर सूची में जोड़ा गया (swept_liquidity key अब यहाँ से हटा दी गई है)
        alerts.append({
            "direction": direction, "pattern": z.patternType, "category": z.zoneCategory,
            "entry": z.proxVal, "sl": z.slVal, "tp": z.tpVal, "is_hq": z.isHQ,
            "score": z.densityScore, "touch_count": z.touchCount, "qty": z.qty,
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    # हाई-क्वालिटी और निकटता (Distance) के आधार पर अलर्ट्स को सॉर्ट किया गया
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts  # सॉर्ट किए गए अलर्ट्स रिटर्न किए गए
