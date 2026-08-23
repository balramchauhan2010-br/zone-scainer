# -*- coding: utf-8 -*-
# फाइल की एन्कोडिंग UTF-8 सेट की गई है ताकि हिंदी व अन्य कैरेक्टर्स सही से प्रोसेस हों

"""
zone_core.py — v8.8 (SMART Overnight/Multi-Day Gap Fix — Date-Aware Imbalance Cap)
यह एक एडवांस्ड डिमांड और सप्लाई (D&S) ज़ोन डिटेक्शन इंजन है।

=== v8.6 से v8.8 में क्या ठीक किया गया (ICICI Bank जैसे OVERNIGHT-GAP zones) ===
आपने बिल्कुल सही पकड़ा — यह ज़ोन असल में ALAG DATE का मामला था:
    Leg-In  : 23 Jun 2026, 2:15pm   (TR=13.70)
    Base    : 23 Jun 2026, 3:15pm   (TR=6.60, दिन की आख़िरी कैंडल)
    Leg-Out : 24 Jun 2026, 9:15am   (TR=35, अगले ट्रेडिंग-दिन की पहली कैंडल)

Base और Leg-Out के बीच रात भर मार्केट बंद रहा — इसलिए यह एक असली
OVERNIGHT GAP है (intraday gap नहीं)। पुराने कोड में gap की अधिकतम सीमा
(cap) हमेशा सिर्फ़ Leg-In TR (13.70) के बराबर रखी जाती थी:

    gapSize <= maxImbalanceVsLegInMult * legInTR   (डिफ़ॉल्ट 1.0x = 13.70)

लेकिन overnight gap का असली साइज़ (legOutLow - maxBaseHigh) स्वाभाविक रूप
से इससे बड़ा निकल सकता है (जैसा इस उदाहरण में हुआ, legOutTR ही 35 है) —
और यह पूरी तरह GENUINE/संस्थागत signal है, कोई गड़बड़ी नहीं। पुराना कोड
इसे गलती से "बहुत बड़ा अविश्वसनीय gap" मानकर reject कर देता था।

FIX (v8.8) — अब हर Leg-Out कैंडल के लिए df.index (timestamp) से यह पता
लगाया जाता है कि क्या वह अपनी ठीक पहले वाली Base कैंडल से अलग तारीख
(date) की है:

    isOvernightGap = date(LegOut) != date(Base का ठीक पिछला candle)

  - अगर isOvernightGap == True  -> gap-size पर कोई cap नहीं लगाया जाता
                                     (overnight gap खुद अपनी वैधता साबित
                                     करता है) -> साथ ही एक्स्ट्रा स्कोर
                                     बोनस (overnightGapScoreBonus) मिलता है।
  - अगर isOvernightGap == False (यानी वही दिन, intraday gap) -> पुरानी
                                     सुरक्षा (legInTR-आधारित cap) जस-की-तस
                                     लागू रहती है, ताकि कोई भी पहले से
                                     valid/invalid हो रहा zone प्रभावित ना हो।

अगर df.index DatetimeIndex नहीं है (timestamp उपलब्ध नहीं), तो कोड
सुरक्षित रूप से पुराने (intraday) व्यवहार पर वापस चला जाता है — यानी
यह बदलाव सिर्फ़ additive/safe है, कहीं भी पुराना सही व्यवहार नहीं तोड़ता।

(बाकी सभी v8.5/v8.6 के नियम बिना किसी बदलाव के जस-के-तस रखे गए हैं)

------------------------------------------------------------------
FULL VALIDATION (v8.8)
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
        + gap size cap:
            SAME-DAY (intraday) gap  -> legIn TR से बड़ा नहीं हो
            OVERNIGHT/multi-day gap  -> कोई cap नहीं (genuine)        [NEW v8.8]

  SCORE:
    - densityScore < 40  -> zone सिरे से invalid (discard)
    - densityScore >= 90 -> High-Quality (HQ) zone
    - Leg-Out >= 2.0x Leg-In TR -> बोनस (hqLegOutTrMult)
    - DBR में legOut का body-position OR body% (heavy buying pressure) -> बोनस
    - असली intraday प्राइस-गैप मौजूद होने पर -> बोनस (genuineGapScoreBonus)
    - असली OVERNIGHT/multi-day गैप मौजूद होने पर -> अतिरिक्त बोनस      [NEW v8.8]

  STATE:
    - Tested तभी बने जब price, LEG-OUT कैंडल के 50% area (या इससे ज़्यादा) तक
      वापस retrace कर आए
    - touchCount > 2 होने पर zone अपने-आप Broken हो जाती है

Public entry points:
    scan_zones(df, params=None, lookback_months=None) -> List[Zone]
    latest_active_zones(zones, ...)                    -> List[Zone]
    get_zone_alerts(zones, current_price, ..)          -> List[dict]
    diagnose_bar(df, at_index, params=None)            -> List[dict]   (debug helper)
"""

from dataclasses import dataclass  # डेटा स्ट्रक्चर को आसानी से क्लास के रूप में डिफाइन करने के लिए dataclass मॉड्यूल
from typing import List, Optional, Dict, Any  # टाइप हिंटिंग (Type Hints) के लिए आवश्यक डेटा टाइप्स
import numpy as np  # तेज़ एरे ऑपरेशन्स और गणितीय गणना के लिए NumPy
import pandas as pd  # टाइम-सीरीज़ और टेक्निकल डेटा प्रोसेसिंग के लिए Pandas


# सिस्टम के डिफ़ॉल्ट पैरामीटर्स की डिक्शनरी (मशीन/अल्गो की मूल सेटिंग्स)
DEFAULT_PARAMS = dict(
    # --- कैपिटल और रिस्क सेटिंग्स ---
    accountCapital=25000.0,   # खाता की कुल पूँजी — SL/TP गणना के संदर्भ के लिए
    riskPct=0.5,              # प्रति ट्रेड लिया जाने वाला रिस्क प्रतिशत (0.5%)
    targetRR=5.0,             # रिस्क-टू-रिवॉर्ड अनुपात लक्ष्य (1:5)
    slBufferAtr=0.1,          # स्टॉपलॉस में ATR का अतिरिक्त बफर (0.1x ATR)

    # --- एल्गोरिदम और फिल्टर्स ---
    atrPeriod=14,             # ATR (Average True Range) इंडिकेटर की अवधि (14 कैंडल्स)
    volSmaPeriod=20,          # औसतन वॉल्यूम निकालने की अवधि (20 कैंडल्स का SMA)
    legOutTrMult=1.2,         # Leg-Out कैंडल का ATR-आधारित मल्टीप्लायर (explosive)
    legOutMinTrRatio=1.0,     # Leg-Out TR, Leg-In TR का कम से कम इतना गुना (validity)
    hqLegOutTrMult=2.0,       # हाई-क्वालिटी Leg-Out कैंडल का TR मल्टीप्लायर (सिर्फ़ स्कोरिंग बोनस)
    hqLegInAtrMult=1.5,       # हाई-क्वालिटी Leg-In कैंडल का TR, ATR का 1.5x
    maxBaseAtrMult=1.0,       # बेस कैंडल में सबसे बड़े TR वाली कैंडल, ATR के इतने गुना से छोटी हो
    maxWickPct=0.25,          # Leg-Out कैंडल में अधिकतम विक/शैडो % (25%)

    # --- बेस और लेग-इन नियम ---
    minBaseCount=1,             # ज़ोन में कम से कम बेस कैंडल्स (1)
    maxBaseCount=3,             # ज़ोन में अधिकतम बेस कैंडल्स (3)
    legInMinAtrMult=1.0,        # Leg-In कैंडल का TR कम से कम ATR के बराबर (1.0x) होना चाहिए
    minClvPct=0.60,             # Leg-In कैंडल का न्यूनतम Close Location Value (60%)
    legInToBaseSizeMult=2.0,    # Leg-In कैंडल सबसे बड़ी बेस कैंडल से कम से कम 2x बड़ी होनी चाहिए
    legInMinBodyPct=0.60,       # Leg-In कैंडल की BODY, उसकी कुल रेंज का कम से कम 60% होनी चाहिए

    # --- इमबैलेंस सेटिंग्स ---
    useImbalance=True,             # प्राइस इमबैलेंस (Gap/Fast Move) चेक करें (validity filter)
    maxImbalanceVsLegInMult=1.0,   # SAME-DAY (intraday) gap की सीमा leg-in TR का इतना गुना (1.0x)
    relaxGapCapOnOvernight=True,   # [NEW v8.8] अगर Leg-Out अलग तारीख (overnight/multi-day) की
                                     # कैंडल है, तो gap-size cap हटा दिया जाए (genuine institutional gap)
    genuineGapScoreBonus=10,       # असली intraday प्राइस-गैप मौजूद होने पर मिलने वाला स्कोर बोनस
    overnightGapScoreBonus=15,     # [NEW v8.8] असली OVERNIGHT/multi-day gap मौजूद होने पर
                                     # मिलने वाला अतिरिक्त स्कोर बोनस (institutional-grade signal)

    # --- विपरीत-रंग वाली पीछे की candle का filter (सिर्फ़ BODY पर आधारित) ---
    rejectOppositeCoverPct=0.50,   # पीछे वाली opposite-color candle की BODY, leg-in रेंज का
                                     # >=50% cover करे तो zone invalid

    # --- डेंसिटी स्कोर थ्रेशोल्ड ---
    minValidScore=40,              # इससे कम स्कोर वाला zone सिरे से invalid माना जाता है
    hqScoreThreshold=90,           # इतने या इससे ज़्यादा स्कोर वाला zone High-Quality (HQ) माना जाता है

    # --- DBR में leg-out के लिए "heavy buying pressure" वैकल्पिक शर्त ---
    legOutBodyHeavyPressurePct=0.60,

    # --- Tested-state और re-touch invalidation ---
    testedLegOutRetracePct=0.50,
    maxTestedCount=2,
)

_HARD_MAX_BASE_COUNT = 3  # कोड में बेस कैंडल की अधिकतम सीमा 3 पर लॉक की गई है


# ज़ोन के सभी प्रॉपर्टीज को स्टोर करने के लिए ज़ोन डेटा क्लास
@dataclass
class Zone:
    proxVal: float
    distVal: float
    slVal: float
    tpVal: float
    isDemand: bool
    isHQ: bool
    densityScore: int
    patternType: str = ""
    zoneCategory: str = ""
    state: str = "Fresh"
    touchCount: int = 0
    originalDensityScore: int = 0
    startBarIndex: int = 0
    createdBarIndex: int = 0
    baseCount: int = 0
    timestamp: object = None
    legOutHigh: float = 0.0
    legOutLow: float = 0.0
    legOutMidLevel: float = 0.0
    isOvernightGap: bool = False   # [NEW v8.8] डिबग/जानकारी के लिए — क्या यह ज़ोन overnight gap पर बनी


# Wilder's Smoothing विधि द्वारा ATR (Average True Range) की गणना करने वाला फ़ंक्शन
def _wilder_atr(high, low, close, period):
    n = len(high)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    if n > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum(
            high[1:] - low[1:],
            np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
        )
    atr = np.full(n, np.nan)
    if n >= period:
        seed = tr[:period].mean()
        atr[period - 1] = seed
        if n > period:
            alpha = 1.0 / period
            tail = pd.Series(tr[period:])
            seeded = pd.concat([pd.Series([seed]), tail], ignore_index=True)
            smoothed = seeded.ewm(alpha=alpha, adjust=False).mean().to_numpy()
            atr[period:] = smoothed[1:]
    return atr


def _resolve_start_bar_for_lookback(df: pd.DataFrame, lookback_months: Optional[float]) -> int:
    n = len(df)
    if lookback_months is None or lookback_months <= 0 or n == 0:
        return 0
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        cutoff = idx[-1] - pd.DateOffset(months=lookback_months)
        pos = idx.searchsorted(cutoff, side="left")
        return int(max(0, pos))
    approx_bars = int(round(lookback_months * 21))
    return int(max(0, n - approx_bars))


def _bar_dates_array(df: pd.DataFrame):
    """
    [NEW v8.8] हर कैंडल की सिर्फ़ 'date' (दिन, बिना समय के) निकालकर एक NumPy array
    के रूप में लौटाता है — ताकि दो कैंडल्स की तारीख compare करके यह पता लगाया जा
    सके कि उनके बीच overnight/multi-day gap है या नहीं। अगर df.index DatetimeIndex
    नहीं है, तो None लौटाया जाता है (caller तब safely पुराने behaviour पर वापस चला
    जाता है, यानी हमेशा "same-day" मान लिया जाता है)।
    """
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        return idx.date  # numpy array of datetime.date objects
    # कोशिश करें कि शायद index datetime-like strings/objects हों, coercible हों
    try:
        parsed = pd.to_datetime(idx)
        return parsed.date
    except Exception:
        return None


# --------------------------------------------------------------------------
# मुख्य स्कैनिंग इंजन (Core Scan Function) - ज़ोन ढूंढने का मुख्य फ़ंक्शन
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
               lookback_months: Optional[float] = None) -> List[Zone]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    n = len(df)

    minBaseCount = p["minBaseCount"]
    maxBaseCount = p["maxBaseCount"]
    atrPeriod = p["atrPeriod"]

    atr = _wilder_atr(h, l, c, atrPeriod)
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()

    # [NEW v8.8] हर कैंडल की तारीख (date-only) निकाली गई — overnight-gap पहचान के लिए
    bar_dates = _bar_dates_array(df)

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

            # ---------------- [NEW v8.8] OVERNIGHT/MULTI-DAY GAP पहचान ----------------
            # Leg-Out (t) और उसकी ठीक पहले वाली Base कैंडल (t-1) की तारीख compare
            # की जाती है। अगर तारीख अलग है -> यह overnight/multi-day gap है (जैसे
            # आपका ICICI Bank उदाहरण: Base 23 Jun 3:15pm, LegOut 24 Jun 9:15am)।
            isOvernightGap = False
            if bar_dates is not None:
                try:
                    isOvernightGap = bar_dates[t] != bar_dates[t - 1]
                except Exception:
                    isOvernightGap = False

            # ---------------- प्राइस इमबैलेंस चेकिंग (date-aware gap-cap) ----------------
            hasImbalance = True
            hasGenuineGap = False
            gapSize = 0.0

            legInCap = p["maxImbalanceVsLegInMult"] * legInTR
            if isOvernightGap and p.get("relaxGapCapOnOvernight", True):
                # overnight/multi-day gap: कोई कृत्रिम cap नहीं — genuine institutional gap
                gapCap = float("inf")
            else:
                # same-day (intraday) gap: पुरानी सुरक्षा जस-की-तस लागू
                gapCap = legInCap

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

            densityScore += 10  # Fresh Zone बोनस

            if hasGenuineGap:
                densityScore += p["genuineGapScoreBonus"]

            # [NEW v8.8] genuine overnight/multi-day gap होने पर अतिरिक्त बोनस
            if isOvernightGap and hasGenuineGap:
                densityScore += p["overnightGapScoreBonus"]

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
                isOvernightGap=isOvernightGap,
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


def latest_active_zones(zones: List[Zone], include_tested: bool = True) -> List[Zone]:
    states = {"Fresh"} | ({"Tested"} if include_tested else set())
    return [z for z in zones if z.state in states]


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
            "is_overnight_gap": z.isOvernightGap,
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts


# --------------------------------------------------------------------------
# डायग्नोस्टिक/ट्रबलशूटिंग हेल्पर (डिबग के लिए) — v8.8 gap-cap logic parity
# --------------------------------------------------------------------------
def diagnose_bar(df: pd.DataFrame, at_index, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    """
    किसी specific candle को Leg-Out मानकर (baseCount=1,2,3 तीनों आज़माकर) हर नियम
    का pass/fail बताता है, साथ ही यह भी बताता है कि isOvernightGap True है या False
    और gapCap क्या इस्तेमाल हुआ — ताकि भविष्य में "यह zone स्कैन क्यों नहीं हुई"
    जैसे सवाल का जवाब सीधे मिल जाए।
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    atr = _wilder_atr(h, l, c, p["atrPeriod"])
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()
    bar_dates = _bar_dates_array(df)

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

        isOvernightGap = False
        if bar_dates is not None:
            try:
                isOvernightGap = bar_dates[t] != bar_dates[t - 1]
            except Exception:
                isOvernightGap = False
        rep["isOvernightGap"] = isOvernightGap

        legInCap = p["maxImbalanceVsLegInMult"] * legInTR
        gapCap = float("inf") if (isOvernightGap and p.get("relaxGapCapOnOvernight", True)) else legInCap
        rep["gapCap_used"] = gapCap

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
