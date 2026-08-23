# -*- coding: utf-8 -*-
# फाइल की एन्कोडिंग UTF-8 सेट की गई है ताकि हिंदी व अन्य कैरेक्टर्स सही से प्रोसेस हों

"""
zone_core.py — v8.7 (Gap-Cap Fix: Overnight/Genuine Big-Gap Zones + Diagnose Helper)
यह एक एडवांस्ड डिमांड और सप्लाई (D&S) ज़ोन डिटेक्शन इंजन है।

=== v8.6 से v8.7 में क्या बदला (ICICI Bank जैसे genuine बड़े gap वाले zones का FIX) ===
पहले (v8.5/v8.6) में gap/imbalance की size को सिर्फ़ Leg-In के TR के आधार पर
सीमित किया जाता था:
    gapSize <= maxImbalanceVsLegInMult * legInTR   (डिफ़ॉल्ट 1.0x)

समस्या: जब Leg-Out कैंडल खुद बहुत बड़ी/explosive होती है (जैसे ICICI Bank 1h
उदाहरण: LegIn TR=13.70, लेकिन LegOut TR=35 — और यह अगले ट्रेडिंग-दिन की पहली
(9:15am) कैंडल थी, यानी पिछले दिन के Base से इसका असली OVERNIGHT GAP बना), तो
genuine gap का size आसानी से Leg-In TR (13.70) से बड़ा हो जाता है — जो बिल्कुल
सही/वैध situation है (क्योंकि पूरा leg-out ही 35 पॉइंट का उछाल है)। पुराने कोड में
ऐसे genuine बड़े gap को गलती से "बहुत बड़ा/अविश्वसनीय gap" मानकर reject कर दिया
जाता था, जिससे `hasImbalance=False` हो जाता और साथ ही `hasGenuineGap` भी False
होकर engulf-check में भी दोबारा zone को invalid कर देता (डबल नुकसान)।

FIX (v8.7): चूँकि नियम के अनुसार हमेशा `legOutTR >= legInTR` होता है (TR
Hierarchy से गारंटीड), इसलिए अब gap की अधिकतम सीमा (cap) दो में से जो भी बड़ी
हो, उसे मान्य किया जाता है:
    legInCap  = maxImbalanceVsLegInMult  * legInTR    (पुराना, जस-का-तस रखा)
    legOutCap = maxImbalanceVsLegOutMult * legOutTR   (नया, ज़्यादा permissive)
    gapCap    = max(legInCap, legOutCap)
यह बदलाव सिर्फ़ सीमा को बढ़ाता है, घटाता नहीं — इसलिए पहले से valid कोई भी
zone अब invalid नहीं होगा (क्योंकि legOutTR>=legInTR हमेशा सच है, legOutCap
कभी भी legInCap से छोटा नहीं होगा अगर mult बराबर रखें)। इससे सिर्फ़ वो genuine
बड़े-gap वाले valid zones ठीक से पकड़ में आएँगे जो पहले गलती से reject हो रहे थे।

इसके अलावा एक नया डिबग/ट्रबलशूटिंग हेल्पर फ़ंक्शन जोड़ा गया है:
    diagnose_bar(df, at_index, params=None)
यह किसी specific candle (Leg-Out) पर हर नियम का pass/fail स्टेप-बाय-स्टेप
प्रिंट करता है, ताकि भविष्य में "यह वाला zone स्कैन क्यों नहीं हुआ" जैसे सवाल
का जवाब कोड दोबारा पढ़े बिना, सीधे तुरंत मिल जाए।

बाकी पूरा v8.5/v8.6 का लॉजिक (सभी पुराने नियम) जस-का-तस रखा गया है।

------------------------------------------------------------------
FULL VALIDATION (v8.7)
------------------------------------------------------------------
  LEG-IN:
    - correct direction (bull/bear)
    - Body Strength: |Close-Open| / (High-Low) >= 60%
    - Opposite-color पीछे वाली candle की सिर्फ़ BODY leg-in range का 50%+ cover ना करे
    - TR >= ATR
    - TR >= 2.0 x Max Base TR

  BASE (1-3 candles):
    - each candle TR <= ATR

  LEG-OUT:
    - correct direction
    - Explosive: TR >= 1.2 x ATR (legOutTrMult)
    - Wick % <= 25%
    - TR Hierarchy: LegOut >= LegIn > MaxBaseTR       (यह नियम अनिवार्य है)
    - Volume: Volume[legOut] > Volume[legIn]
    - Leg-Out की सिर्फ़ BODY पूरे base-zone (wick सहित) को engulf ना करे
      (जब तक genuine gap ना हो)
    - Imbalance (if useImbalance):
        Demand: Low > MaxBaseHigh  OR  Close > LegInHigh
        Supply: High < MinBaseLow  OR  Close < LegInLow
        + gap का size max(legInCap, legOutCap) से बड़ा नहीं हो   [UPDATED v8.7]

  SCORE:
    - densityScore < 40  -> zone सिरे से invalid (discard)
    - densityScore >= 90 -> High-Quality (HQ) zone
    - Leg-Out >= 2.0x Leg-In TR -> बोनस (hqLegOutTrMult)
    - DBR में legOut का body-position OR body% (heavy buying pressure) -> बोनस
    - असली प्राइस-गैप (genuine imbalance) मौजूद होने पर -> बोनस

  STATE:
    - Tested तभी बने जब price, LEG-OUT कैंडल के 50% area (या इससे ज़्यादा) तक
      वापस retrace कर आए
    - touchCount > 2 होने पर zone अपने-आप Broken हो जाती है

Public entry points:
    scan_zones(df, params=None, lookback_months=None) -> List[Zone]
    latest_active_zones(zones, ...)                    -> List[Zone]
    get_zone_alerts(zones, current_price, ..)          -> List[dict]
    diagnose_bar(df, at_index, params=None)            -> List[dict]   [NEW v8.7]
"""

from dataclasses import dataclass  # डेटा स्ट्रक्चर को आसानी से क्लास के रूप में डिफाइन करने के लिए dataclass मॉड्यूल
from typing import List, Optional, Dict, Any  # टाइप हिंटिंग (Type Hints) के लिए आवश्यक डेटा टाइप्स
import numpy as np  # तेज़ एरे ऑपरेशन्स और गणितीय गणना के लिए NumPy
import pandas as pd  # टाइम-सीरीज़ और टेक्निकल डेटा प्रोसेसिंग के लिए Pandas


# सिस्टम के डिफ़ॉल्ट पैरामीटर्स की डिक्शनरी (मशीन/अल्गो की मूल सेटिंग्स)
DEFAULT_PARAMS = dict(
    # --- कैपिटल और रिस्क सेटिंग्स ---
    accountCapital=25000.0,   # खाता की कुल पूँजी ($25,000) — अब सिर्फ़ SL/TP गणना के संदर्भ के लिए रखा है
    riskPct=0.5,              # प्रति ट्रेड लिया जाने वाला रिस्क प्रतिशत (0.5%)
    targetRR=5.0,             # रिस्क-टू-रिवॉर्ड अनुपात लक्ष्य (1:5)
    slBufferAtr=0.1,          # स्टॉपलॉस में ATR का अतिरिक्त बफर (0.1x ATR)

    # --- एल्गोरिदम और फिल्टर्स ---
    atrPeriod=14,             # ATR (Average True Range) इंडिकेटर की अवधि (14 कैंडल्स)
    volSmaPeriod=20,          # औसतन वॉल्यूम निकालने की अवधि (20 कैंडल्स का SMA)
    legOutTrMult=1.2,         # Leg-Out कैंडल का ATR-आधारित मल्टीप्लायर — Leg-Out का TR
                                # कम से कम इतने गुना ATR (1.2x) होना चाहिए, तभी उसे "explosive" माना जाएगा
    legOutMinTrRatio=1.0,     # Leg-Out TR, Leg-In TR का कम से कम इतना गुना होना
                                # चाहिए (1.0 = बराबर या बड़ा) — validity के लिए
    hqLegOutTrMult=2.0,       # हाई-क्वालिटी Leg-Out कैंडल का TR मल्टीप्लायर (Leg-In TR का 2.0x, सिर्फ़ स्कोरिंग बोनस)
    hqLegInAtrMult=1.5,       # हाई-क्वालिटी Leg-In कैंडल का TR, ATR का 1.5x
    maxBaseAtrMult=1.0,       # बेस कैंडल में जिस भी base कैंडल का TR सबसे बड़ा हो, वो ATR के
                                # इतने गुना (1.0x) से छोटा होना चाहिए (यानी base, ATR से बड़ा TR ना दे)
    maxWickPct=0.25,          # Leg-Out कैंडल में अधिकतम विक/शैडो % (25%)

    # --- बेस और लेग-इन नियम ---
    minBaseCount=1,             # ज़ोन में कम से कम बेस कैंडल्स (1)
    maxBaseCount=3,             # ज़ोन में अधिकतम बेस कैंडल्स (3)
    legInMinAtrMult=1.0,        # Leg-In कैंडल का TR कम से कम ATR के बराबर (1.0x) होना चाहिए
    minClvPct=0.60,             # Leg-In कैंडल का न्यूनतम Close Location Value (60%)
    legInToBaseSizeMult=2.0,    # Leg-In कैंडल सबसे बड़ी बेस कैंडल से कम से कम 2x बड़ी होनी चाहिए
    legInMinBodyPct=0.60,       # Leg-In कैंडल की BODY, उसकी कुल रेंज (High-Low) का कम से कम 60% होनी चाहिए

    # --- इमबैलेंस सेटिंग्स ---
    useImbalance=True,             # प्राइस इमबैलेंस (Gap/Fast Move) चेक करें (validity filter)
    maxImbalanceVsLegInMult=1.0,   # [पुराना cap] gap/imbalance का साइज़ leg-in TR से बड़ा नहीं होना चाहिए (1.0x)
    maxImbalanceVsLegOutMult=1.0,  # [NEW v8.7] gap/imbalance का दूसरा (ज़्यादा permissive) cap — leg-out
                                     # TR के आधार पर। असल cap = max(legInMult*legInTR, legOutMult*legOutTR)।
                                     # यह overnight/genuine बड़े gap वाले zones (जैसे ICICI Bank उदाहरण,
                                     # legOutTR=35 vs legInTR=13.70) को गलती से reject होने से बचाता है।
    genuineGapScoreBonus=10,       # असली प्राइस-गैप (सिर्फ़ close-based नहीं, वाकई gap) मौजूद
                                     # होने पर मिलने वाला डेंसिटी-स्कोर बोनस

    # --- विपरीत-रंग वाली पीछे की candle का filter (सिर्फ़ BODY पर आधारित) ---
    rejectOppositeCoverPct=0.50,   # अगर leg-in के ठीक पीछे वाली opposite-color candle
                                     # की BODY, leg-in की रेंज का >=50% cover करे,
                                     # तो zone invalid (सिर्फ़ body, पूरी wick range नहीं)

    # --- डेंसिटी स्कोर थ्रेशोल्ड ---
    minValidScore=40,              # इससे कम स्कोर वाला zone सिरे से invalid माना जाता है
    hqScoreThreshold=90,           # इतने या इससे ज़्यादा स्कोर वाला zone High-Quality (HQ) माना जाता है

    # --- DBR में leg-out के लिए "heavy buying pressure" वैकल्पिक शर्त ---
    legOutBodyHeavyPressurePct=0.60,  # DBR pattern में legOut की body इतने % (range का) से बड़ी हो तो
                                        # body-position चेक (>=80% close) के बराबर ही अंक मिल जाते हैं

    # --- Tested-state और re-touch invalidation ---
    testedLegOutRetracePct=0.50,   # zone को "Tested" मानने के लिए price को LEG-OUT
                                     # कैंडल की खुद की रेंज में कम से कम इतनी गहराई (50%) तक वापस आना
                                     # होगा (legOutHigh/legOutLow के बीच के 50% स्तर पर आधारित)
    maxTestedCount=2,               # zone को इससे ज़्यादा बार टेस्ट होने पर (touchCount > 2)
                                     # अपने-आप invalid/Broken मान लिया जाता है
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
    state: str = "Fresh"            # स्थिति: Fresh, Tested (leg-out रेंज के 50%+ retracement पर), Broken
    touchCount: int = 0             # प्राइस द्वारा ज़ोन को टेस्ट करने की संख्या
    originalDensityScore: int = 0   # ज़ोन निर्माण के समय का शुरुआती स्कोर
    startBarIndex: int = 0          # ज़ोन शुरू होने वाली कैंडल का इंडेक्स
    createdBarIndex: int = 0        # ज़ोन पूरा बनने वाली कैंडल (Leg-Out) का इंडेक्स
    baseCount: int = 0              # ज़ोन में शामिल बेस कैंडल्स की संख्या
    timestamp: object = None        # ज़ोन बनने की तिथि व समय
    legOutHigh: float = 0.0         # Leg-Out कैंडल का High (Tested-level निकालने के लिए)
    legOutLow: float = 0.0          # Leg-Out कैंडल का Low
    legOutMidLevel: float = 0.0     # Leg-Out रेंज का testedLegOutRetracePct स्तर (Tested के लिए)


# Wilder's Smoothing विधि द्वारा ATR (Average True Range) की गणना करने वाला फ़ंक्शन
def _wilder_atr(high, low, close, period):
    n = len(high)  # कैंडल्स की कुल संख्या
    tr = np.empty(n)  # True Range के लिए खाली NumPy सरणी (Array)
    tr[0] = high[0] - low[0]  # पहली कैंडल का TR (High - Low)
    if n > 1:
        prev_close = close[:-1]  # पिछली कैंडल का Close प्राइस
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


def _prep_arrays(df, p):
    """स्कैन और डायग्नोसिस दोनों में इस्तेमाल होने वाली common array-prep (कोड डुप्लीकेशन से बचने के लिए)."""
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    atr = _wilder_atr(h, l, c, p["atrPeriod"])
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()
    return o, h, l, c, v, atr, vol_sma


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

    o, h, l, c, v, atr, vol_sma = _prep_arrays(df, p)
    n = len(df)  # कैंडल की कुल संख्या

    minBaseCount = p["minBaseCount"]  # न्यूनतम बेस काउंट (लोकल वेरिएबल में कॉपी)
    maxBaseCount = p["maxBaseCount"]  # अधिकतम बेस काउंट
    atrPeriod = p["atrPeriod"]        # ATR पीरियड

    # सापेक्ष इंडेक्स पर True Range (TR) निकालने के लिए इंटरनल फ़ंक्शन
    def tr(t, idx):
        return h[t - idx] - l[t - idx]

    def is_bull(t, idx):
        return c[t - idx] > o[t - idx]

    def is_bear(t, idx):
        return o[t - idx] > c[t - idx]

    def wick_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]
        if rng == 0:
            return 0.0
        wicks = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])
        return wicks / rng

    def body_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]
        if rng == 0:
            return 0.0
        body = abs(c[i] - o[i])
        return body / rng

    def body_high_low(t, idx):
        i = t - idx
        return max(o[i], c[i]), min(o[i], c[i])

    zones: List[Zone] = []
    active_zones: List[Zone] = []

    min_start = max(atrPeriod, maxBaseCount + 3, 11)
    record_from_bar = max(min_start, _resolve_start_bar_for_lookback(df, lookback_months))

    legOutMult = p.get("legOutTrMult", p.get("legOutAtrMult", 1.2))

    for t in range(min_start, n):
        if np.isnan(atr[t]):
            continue

        zoneFoundOnThisBar = False

        for baseCount in range(minBaseCount, maxBaseCount + 1):
            if zoneFoundOnThisBar:
                break

            legOutIdx = 0
            legInIdx = baseCount + 1
            prevIdx = legInIdx + 1

            if t - prevIdx < 0 or t - baseCount < 0:
                continue
            if np.isnan(atr[t - legInIdx]) or np.isnan(atr[t]):
                continue

            # ---------------- LEG-IN की जाँच ----------------
            legInTR = tr(t, legInIdx)
            legInLow = l[t - legInIdx]
            legInHigh = h[t - legInIdx]
            legInClose = c[t - legInIdx]
            legInVol = v[t - legInIdx]
            legInRng = legInHigh - legInLow

            legInIsBull = is_bull(t, legInIdx)
            legInIsBear = is_bear(t, legInIdx)

            if legInRng == 0:
                continue

            legInBodyPct = body_pct(t, legInIdx)
            if legInBodyPct < p["legInMinBodyPct"]:
                continue

            prevIsBull = is_bull(t, prevIdx)
            prevIsBear = is_bear(t, prevIdx)
            isOppositeColor = (legInIsBull and prevIsBear) or (legInIsBear and prevIsBull)
            if isOppositeColor:
                prevBodyHigh, prevBodyLow = body_high_low(t, prevIdx)
                overlap = max(0.0, min(prevBodyHigh, legInHigh) - max(prevBodyLow, legInLow))
                coverPct = overlap / legInRng
                if coverPct >= p["rejectOppositeCoverPct"]:
                    continue

            bullClv = (legInClose - legInLow) / legInRng
            bearClv = (legInHigh - legInClose) / legInRng

            # ---------------- BASE की जाँच ----------------
            allBaseValid = True
            maxBaseTR = 0.0
            maxBaseHigh = -1.0
            minBaseLow = float("inf")
            hasOppositeColorBase = False

            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    allBaseValid = False
                    break
                bTR = tr(t, b)

                if bTR > (p["maxBaseAtrMult"] * atr[t - b]):
                    allBaseValid = False
                    break

                if bTR > maxBaseTR:
                    maxBaseTR = bTR

                if h[t - b] > maxBaseHigh:
                    maxBaseHigh = h[t - b]
                if l[t - b] < minBaseLow:
                    minBaseLow = l[t - b]

            if not allBaseValid or maxBaseTR == 0:
                continue

            if legInTR < (p["legInToBaseSizeMult"] * maxBaseTR):
                continue

            validLegIn = legInTR >= (p["legInMinAtrMult"] * atr[t - legInIdx])
            if not validLegIn:
                continue

            # ---------------- LEG-OUT की जाँच ----------------
            legOutTR = tr(t, legOutIdx)
            legOutHigh = h[t - legOutIdx]
            legOutLow = l[t - legOutIdx]
            legOutClose = c[t - legOutIdx]
            legOutOpen = o[t - legOutIdx]
            legOutVol = v[t - legOutIdx]

            isDemandLegOut = is_bull(t, legOutIdx)
            isSupplyLegOut = is_bear(t, legOutIdx)
            if not (isDemandLegOut or isSupplyLegOut):
                continue

            isLegOutExplosive = legOutTR >= (legOutMult * atr[t - legOutIdx])
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["maxWickPct"]
            passesTRHierarchy = (legOutTR >= p["legOutMinTrRatio"] * legInTR) and (legInTR > maxBaseTR)
            passesVolume = legOutVol > legInVol

            # ---------------- [FIXED v8.7] प्राइस इमबैलेंस चेकिंग (dual gap-cap) ----------------
            # gap ki cap ab do sanctions me se jo bhi बड़ी हो, वह मानी जाती है:
            #   legInCap  = maxImbalanceVsLegInMult  * legInTR   (पुराना, backward-compatible)
            #   legOutCap = maxImbalanceVsLegOutMult * legOutTR  (नया, बड़े explosive/gap zones के लिए)
            # चूँकि passesTRHierarchy में legOutTR>=legInTR गारंटीड है, इसलिए legOutCap
            # कभी legInCap से छोटा नहीं होगा (अगर दोनों mult बराबर 1.0 हों) — यानी यह
            # बदलाव सिर्फ़ ज़्यादा उदार (permissive) है, पुराने किसी valid zone को
            # नुकसान नहीं पहुँचाता, सिर्फ़ genuine बड़े gap (overnight जैसे) वाले zones
            # को सही तरीके से पास होने देता है।
            hasImbalance = True
            hasGenuineGap = False
            gapSize = 0.0
            legInCap = p["maxImbalanceVsLegInMult"] * legInTR
            legOutCap = p.get("maxImbalanceVsLegOutMult", 1.0) * legOutTR
            gapCap = max(legInCap, legOutCap)
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasGenuineGap = legOutLow > maxBaseHigh
                    gapCond = hasGenuineGap or (legOutClose > legInHigh)
                    gapSize = max(0.0, legOutLow - maxBaseHigh)
                    hasImbalance = gapCond and (gapSize <= gapCap)
                elif isSupplyLegOut:
                    hasGenuineGap = legOutHigh < minBaseLow
                    gapCond = hasGenuineGap or (legOutClose < legInLow)
                    gapSize = max(0.0, minBaseLow - legOutHigh)
                    hasImbalance = gapCond and (gapSize <= gapCap)
                if hasGenuineGap and gapSize > gapCap:
                    hasGenuineGap = False

            # ---------------- Leg-Out की BODY पूरे base-zone को engulf ना करे (genuine gap हो तो OK) ----------------
            legOutBodyHigh = max(legOutOpen, legOutClose)
            legOutBodyLow = min(legOutOpen, legOutClose)
            legOutBodyEngulfsBase = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)
            if legOutBodyEngulfsBase and not hasGenuineGap:
                continue

            # ---------------- पैटर्न वर्गीकरण (Pattern Classification) ----------------
            isRBR = legInIsBull and (bullClv >= p["minClvPct"]) and isDemandLegOut
            isDBR = legInIsBear and (bearClv >= p["minClvPct"]) and isDemandLegOut
            isDBD = legInIsBear and (bearClv >= p["minClvPct"]) and isSupplyLegOut
            isRBD = legInIsBull and (bullClv >= p["minClvPct"]) and isSupplyLegOut

            isValid = (
                (isRBR or isDBR or isDBD or isRBD)
                and isLegOutExplosive
                and isLegOutWickValid
                and passesTRHierarchy
                and passesVolume
                and hasImbalance
            )

            if not isValid:
                continue

            # ---------------- डेंसिटी स्कोर (Density Score Calculation) ----------------
            densityScore = 0

            if baseCount == 1:
                densityScore += 15

            if legInTR >= (p["hqLegInAtrMult"] * atr[t - legInIdx]):
                densityScore += 10

            if legOutTR >= (p["hqLegOutTrMult"] * tr(t, legInIdx)):
                densityScore += 15

            if (legInTR >= 2.0 * maxBaseTR) and (legOutTR >= 2.0 * legInTR):
                densityScore += 15

            if legOutVol > vol_sma[t - legOutIdx]:
                densityScore += 10

            if isDemandLegOut:
                legOutBodyPos = (legOutClose - legOutLow) / legOutTR if legOutTR > 0 else 0
                legOutOwnBodyPct = body_pct(t, legOutIdx)
                if isDBR:
                    if (legOutBodyPos >= 0.80) or (legOutOwnBodyPct >= p["legOutBodyHeavyPressurePct"]):
                        densityScore += 15
                else:
                    if legOutBodyPos >= 0.80:
                        densityScore += 15
            else:
                legOutBodyPos = (legOutHigh - legOutClose) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.80:
                    densityScore += 15

            for b in range(1, baseCount + 1):
                if isDemandLegOut and is_bear(t, b):
                    hasOppositeColorBase = True
                    break
                elif isSupplyLegOut and is_bull(t, b):
                    hasOppositeColorBase = True
                    break
            if hasOppositeColorBase:
                densityScore += 10

            densityScore += 10

            if hasGenuineGap:
                densityScore += p["genuineGapScoreBonus"]

            if densityScore < p["minValidScore"]:
                continue

            isHQZone = densityScore >= p["hqScoreThreshold"]

            zoneFoundOnThisBar = True

            # ---------------- प्रॉक्सिमल और डिस्टल लाइन्स (Entry/SL/TP) ----------------
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            if isDemandLegOut:
                legOutMidLevel = legOutHigh - p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)
            else:
                legOutMidLevel = legOutLow + p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)

            # ---------------- डुप्लीकेट ज़ोन फिल्टर ----------------
            isDuplicate = False
            checked = 0
            for checkZ in reversed(zones):
                if checkZ.state == "Broken":
                    continue
                if checkZ.isDemand == isDemandLegOut and abs(checkZ.proxVal - proxVal) < (atr[t] * 0.25):
                    isDuplicate = True
                    break
                checked += 1
                if checked >= 11:
                    break
            if isDuplicate:
                continue

            if isRBR:
                patternType, zoneCategory = "RBR", "Continuation"
            elif isDBR:
                patternType, zoneCategory = "DBR", "Reversal"
            elif isDBD:
                patternType, zoneCategory = "DBD", "Continuation"
            else:
                patternType, zoneCategory = "RBD", "Reversal"

            leftBar = t - baseCount

            newZone = Zone(
                proxVal=proxVal, distVal=distVal, slVal=slVal, tpVal=tpVal,
                isDemand=isDemandLegOut, isHQ=isHQZone, densityScore=densityScore,
                patternType=patternType, zoneCategory=zoneCategory, state="Fresh",
                touchCount=0, originalDensityScore=densityScore,
                startBarIndex=leftBar, createdBarIndex=t, baseCount=baseCount,
                timestamp=df.index[t],
                legOutHigh=legOutHigh, legOutLow=legOutLow, legOutMidLevel=legOutMidLevel,
            )
            zones.append(newZone)
            active_zones.append(newZone)

        # ---------------- ज़ोन स्टेटस ट्रैकिंग (Fresh, Tested, Broken) ----------------
        if active_zones:
            lo_t, hi_t = l[t], h[t]
            still_active = []
            for z in active_zones:
                if z.state == "Fresh":
                    if z.isDemand:
                        if lo_t <= z.distVal:
                            z.state = "Broken"
                        elif lo_t <= z.legOutMidLevel:
                            z.state = "Tested"
                            z.touchCount += 1
                    else:
                        if hi_t >= z.distVal:
                            z.state = "Broken"
                        elif hi_t >= z.legOutMidLevel:
                            z.state = "Tested"
                            z.touchCount += 1
                elif z.state == "Tested":
                    if z.isDemand:
                        if lo_t <= z.distVal:
                            z.state = "Broken"
                        elif lo_t <= z.legOutMidLevel:
                            z.touchCount += 1
                    else:
                        if hi_t >= z.distVal:
                            z.state = "Broken"
                        elif hi_t >= z.legOutMidLevel:
                            z.touchCount += 1

                if z.state == "Tested" and z.touchCount > p["maxTestedCount"]:
                    z.state = "Broken"

                if z.state != "Broken":
                    still_active.append(z)
            active_zones = still_active

    if lookback_months is None:
        return zones
    return [z for z in zones if z.createdBarIndex >= record_from_bar]


# केवल एक्टिव (Fresh या Tested) ज़ोन निकालने के लिए हेल्पर फ़ंक्शन
def latest_active_zones(zones: List[Zone], include_tested: bool = True) -> List[Zone]:
    states = {"Fresh"} | ({"Tested"} if include_tested else set())
    return [z for z in zones if z.state in states]


# वर्तमान प्राइस के पास मौजूद ज़ोन के लिए लाइव अलर्ट जनरेट करने वाला फ़ंक्शन
def get_zone_alerts(zones, current_price, min_proximity_pct=0.0, max_proximity_pct=1.0,
                     include_tested=True) -> List[Dict[str, Any]]:
    alerts = []
    candidates = latest_active_zones(zones, include_tested=include_tested)
    for z in candidates:
        if z.proxVal <= 0:
            continue
        if z.isDemand:
            diff_pct = (current_price - z.proxVal) / z.proxVal
            direction = "DEMAND"
        else:
            diff_pct = (z.proxVal - current_price) / z.proxVal
            direction = "SUPPLY"
        if not (min_proximity_pct <= diff_pct <= max_proximity_pct):
            continue
        alerts.append({
            "direction": direction, "pattern": z.patternType, "category": z.zoneCategory,
            "entry": z.proxVal, "sl": z.slVal, "tp": z.tpVal, "is_hq": z.isHQ,
            "score": z.densityScore, "touch_count": z.touchCount,
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts


# --------------------------------------------------------------------------
# [NEW v8.7] डायग्नोस्टिक/ट्रबलशूटिंग हेल्पर
# --------------------------------------------------------------------------
def diagnose_bar(df: pd.DataFrame, at_index, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    """
    किसी specific candle को Leg-Out मानकर (baseCount=1,2,3 तीनों आज़माकर) हर नियम
    का pass/fail स्टेप-बाय-स्टेप बताता है — ताकि भविष्य में "यह zone स्कैन क्यों
    नहीं हुई" जैसे सवाल का जवाब सीधे मिल जाए, बिना पूरा scan_zones दोबारा पढ़े।

    at_index: या तो integer positional index (df में candle की position), या
              df.index में मौजूद कोई timestamp/label (जैसे pd.Timestamp)।

    Return: हर baseCount (1,2,3) के लिए एक dict की list, जिसमें हर नियम का
            True/False status और उससे जुड़े raw नंबर (TR, ATR, gap आदि) होंगे।
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    o, h, l, c, v, atr, vol_sma = _prep_arrays(df, p)
    n = len(df)

    if isinstance(at_index, (int, np.integer)):
        t = int(at_index)
    else:
        t = int(df.index.get_loc(at_index))

    def tr(idx_from_t):
        return h[t - idx_from_t] - l[t - idx_from_t]

    def is_bull(idx_from_t):
        return c[t - idx_from_t] > o[t - idx_from_t]

    def is_bear(idx_from_t):
        return o[t - idx_from_t] > c[t - idx_from_t]

    def wick_pct(idx_from_t):
        i = t - idx_from_t
        rng = h[i] - l[i]
        if rng == 0:
            return 0.0
        wicks = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])
        return wicks / rng

    def body_pct(idx_from_t):
        i = t - idx_from_t
        rng = h[i] - l[i]
        if rng == 0:
            return 0.0
        return abs(c[i] - o[i]) / rng

    legOutMult = p.get("legOutTrMult", p.get("legOutAtrMult", 1.2))
    reports = []

    for baseCount in range(p["minBaseCount"], p["maxBaseCount"] + 1):
        rep: Dict[str, Any] = {"baseCount": baseCount, "legOutTimestamp": df.index[t]}
        legOutIdx = 0
        legInIdx = baseCount + 1
        prevIdx = legInIdx + 1

        if t - prevIdx < 0 or t - baseCount < 0 or np.isnan(atr[t]) or np.isnan(atr[t - legInIdx]):
            rep["result"] = "SKIP (डेटा/ATR अपर्याप्त)"
            reports.append(rep)
            continue

        legInTR = tr(legInIdx)
        legInLow = l[t - legInIdx]; legInHigh = h[t - legInIdx]; legInClose = c[t - legInIdx]
        legInVol = v[t - legInIdx]; legInRng = legInHigh - legInLow
        legInIsBull, legInIsBear = is_bull(legInIdx), is_bear(legInIdx)

        rep["legInTR"] = legInTR
        rep["legInATR"] = atr[t - legInIdx]
        rep["legIn_TR_gte_ATR"] = legInTR >= (p["legInMinAtrMult"] * atr[t - legInIdx])

        if legInRng == 0:
            rep["result"] = "INVALID (legInRng=0)"
            reports.append(rep)
            continue

        rep["legInBodyPct"] = body_pct(legInIdx)
        rep["legIn_body_ok"] = rep["legInBodyPct"] >= p["legInMinBodyPct"]

        bullClv = (legInClose - legInLow) / legInRng
        bearClv = (legInHigh - legInClose) / legInRng
        rep["bullClv"] = bullClv
        rep["bearClv"] = bearClv

        maxBaseTR = 0.0; maxBaseHigh = -1.0; minBaseLow = float("inf"); allBaseValid = True
        for b in range(1, baseCount + 1):
            if np.isnan(atr[t - b]):
                allBaseValid = False
                break
            bTR = tr(b)
            if bTR > (p["maxBaseAtrMult"] * atr[t - b]):
                allBaseValid = False
            if bTR > maxBaseTR:
                maxBaseTR = bTR
            if h[t - b] > maxBaseHigh:
                maxBaseHigh = h[t - b]
            if l[t - b] < minBaseLow:
                minBaseLow = l[t - b]

        rep["maxBaseTR"] = maxBaseTR
        rep["base_all_valid(<=ATR)"] = allBaseValid
        rep["legIn_gte_2xBase"] = legInTR >= (p["legInToBaseSizeMult"] * maxBaseTR) if maxBaseTR else False

        legOutTR = tr(legOutIdx)
        legOutHigh = h[t - legOutIdx]; legOutLow = l[t - legOutIdx]
        legOutClose = c[t - legOutIdx]; legOutOpen = o[t - legOutIdx]; legOutVol = v[t - legOutIdx]
        isDemandLegOut, isSupplyLegOut = is_bull(legOutIdx), is_bear(legOutIdx)

        rep["legOutTR"] = legOutTR
        rep["legOutATR"] = atr[t - legOutIdx]
        rep["legOut_explosive(>=1.2xATR)"] = legOutTR >= (legOutMult * atr[t - legOutIdx])
        rep["legOut_wickPct"] = wick_pct(legOutIdx)
        rep["legOut_wick_ok(<=25%)"] = rep["legOut_wickPct"] <= p["maxWickPct"]
        rep["TR_hierarchy_ok(legOut>=legIn>base)"] = (legOutTR >= p["legOutMinTrRatio"] * legInTR) and (legInTR > maxBaseTR)
        rep["volume_ok(legOut>legIn)"] = legOutVol > legInVol

        legInCap = p["maxImbalanceVsLegInMult"] * legInTR
        legOutCap = p.get("maxImbalanceVsLegOutMult", 1.0) * legOutTR
        gapCap = max(legInCap, legOutCap)
        hasGenuineGap = False; gapSize = 0.0; hasImbalance = True
        if p["useImbalance"]:
            if isDemandLegOut:
                hasGenuineGap = legOutLow > maxBaseHigh
                gapCond = hasGenuineGap or (legOutClose > legInHigh)
                gapSize = max(0.0, legOutLow - maxBaseHigh)
                hasImbalance = gapCond and (gapSize <= gapCap)
            elif isSupplyLegOut:
                hasGenuineGap = legOutHigh < minBaseLow
                gapCond = hasGenuineGap or (legOutClose < legInLow)
                gapSize = max(0.0, minBaseLow - legOutHigh)
                hasImbalance = gapCond and (gapSize <= gapCap)

        rep["gapSize"] = gapSize
        rep["gapCap(legIn/legOut)"] = f"{legInCap:.2f} / {legOutCap:.2f} -> used {gapCap:.2f}"
        rep["hasGenuineGap"] = hasGenuineGap
        rep["imbalance_ok"] = hasImbalance

        legOutBodyHigh = max(legOutOpen, legOutClose); legOutBodyLow = min(legOutOpen, legOutClose)
        engulf = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)
        rep["engulfsBase"] = engulf
        rep["engulf_ok"] = (not engulf) or hasGenuineGap

        isRBR = legInIsBull and (bullClv >= p["minClvPct"]) and isDemandLegOut
        isDBR = legInIsBear and (bearClv >= p["minClvPct"]) and isDemandLegOut
        isDBD = legInIsBear and (bearClv >= p["minClvPct"]) and isSupplyLegOut
        isRBD = legInIsBull and (bullClv >= p["minClvPct"]) and isSupplyLegOut
        rep["pattern"] = "RBR" if isRBR else "DBR" if isDBR else "DBD" if isDBD else "RBD" if isRBD else "NONE"

        rep["FINAL_VALID"] = bool(
            rep["legIn_TR_gte_ATR"] and rep.get("legIn_body_ok") and rep.get("legIn_gte_2xBase")
            and allBaseValid and rep["pattern"] != "NONE"
            and rep["legOut_explosive(>=1.2xATR)"] and rep["legOut_wick_ok(<=25%)"]
            and rep["TR_hierarchy_ok(legOut>=legIn>base)"] and rep["volume_ok(legOut>legIn)"]
            and rep["imbalance_ok"] and rep["engulf_ok"]
        )
        reports.append(rep)

    return reports
