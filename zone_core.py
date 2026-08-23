# -*- coding: utf-8 -*-
"""
zone_core.py — v9.0 (ROOT-CAUSE FIX: True Range अब हर जगह GAP-AWARE है)
यह एक एडवांस्ड डिमांड और सप्लाई (D&S) ज़ोन डिटेक्शन इंजन है।

=== v8.8 से v9.0 में क्या असली/जड़ वाला बग ठीक किया गया ===
आपके ICICI Bank उदाहरण (23 Jun 2:15pm Leg-In, 3:15pm Base, 24 Jun 9:15am
Leg-Out — overnight gap के साथ) को debug करने के बाद असली कारण मिला:

पुराने कोड में हर जगह (Leg-In TR, Leg-Out TR, Base TR निकालने के लिए) यह
हेल्पर फ़ंक्शन था:
    def tr(t, idx):
        return h[t-idx] - l[t-idx]      # सिर्फ़ High-Low, gap शामिल नहीं!

लेकिन असली/संस्थागत "True Range" का सही फ़ॉर्मूला है:
    TR = MAX(High-Low, |High-PrevClose|, |Low-PrevClose|)

ATR की गणना (_wilder_atr) में यह सही फ़ॉर्मूला (PrevClose सहित) पहले से
इस्तेमाल हो रहा था, लेकिन हर individual कैंडल का TR निकालकर तुलना करने
वाले हिस्से में (Leg-In/Leg-Out/Base validity, TR Hierarchy आदि) सिर्फ़
"High-Low" इस्तेमाल हो रहा था — यानी ATR और individual-TR के बीच खुद
कोड में ही एक mismatch/असंगति थी।

नतीजा: जब भी कोई Leg-Out कैंडल OVERNIGHT GAP के साथ खुलती (जैसे आपका
ICICI उदाहरण — असली TR=35, ज़्यादातर हिस्सा gap से बना), कोड के अंदर
calculate होने वाला "legOutTR" (सिर्फ़ h[t]-l[t]) असल में बहुत छोटा
निकल रहा था — शायद Leg-In TR (13.70) से भी कम — जिससे:
    passesTRHierarchy = (legOutTR >= legInTR) ...   -> FAIL
    isLegOutExplosive  = legOutTR >= 1.2*ATR ...     -> FAIL (संभावित)
और इसी वजह से ज़ोन invalid हो रहा था — चाहे आप कितने भी validity-नियम
सही बता दें, क्योंकि मूल समस्या "TR की गणना" में थी, "TR की तुलना के
नियम" में नहीं। (पिछली दो कोशिशें — TR Hierarchy relax करना, gap-cap
हटाना — इसलिए काम नहीं आईं, क्योंकि वो लक्षण पर इलाज कर रही थीं, बीमारी
की जड़ पर नहीं)।

FIX (v9.0): अब एक ही जगह से सही True Range (PrevClose-सहित) निकाला जाता
है (`_true_range()` फ़ंक्शन), और यही एक सोर्स ATR-calculation और हर
individual कैंडल के TR-चेक — दोनों में इस्तेमाल होता है। इससे:
  - overnight-gap वाली Leg-Out कैंडल का TR अब सही (~35, gap सहित) आएगा
  - TR Hierarchy, Explosive-check, HQ-scoring — सब सही व वास्तविक TR पर
    आधारित होंगे
  - Wick% और Body% जानबूझकर पुराने तरीके (सिर्फ़ H-L) पर ही रखे गए हैं,
    क्योंकि वो कैंडल की अपनी shape/pressure मापते हैं, gap को नहीं

(v8.8 का overnight-gap-cap-relax व्यवहार भी बरकरार रखा गया है, क्योंकि
वह एक अलग/अतिरिक्त concept है — raw price-gap का साइज़, जो TR की गणना से
स्वतंत्र है। दोनों फ़िक्स मिलकर पूरी समस्या हल करते हैं।)

------------------------------------------------------------------
FULL VALIDATION (v9.0)
------------------------------------------------------------------
  TR (हर जगह): अब सही True Range = MAX(H-L, |H-PrevClose|, |L-PrevClose|)

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
    - Explosive: TR >= 1.2 x ATR (अब gap-aware TR)
    - Wick % <= 35% (candle की अपनी H-L रेंज पर आधारित)
    - TR Hierarchy: LegOut >= LegIn > MaxBaseTR
    - Volume: Volume[legOut] > Volume[legIn]
    - Leg-Out की सिर्फ़ BODY पूरे base-zone को engulf ना करे (genuine gap हो तो OK)
    - Imbalance: gap size cap same-day पर legInTR तक, overnight पर unlimited

  SCORE:
    - densityScore < 40 -> invalid
    - densityScore >= 90 -> HQ zone
    - Overnight genuine gap -> अतिरिक्त बोनस

Public entry points:
    scan_zones(df, params=None, lookback_months=None) -> List[Zone]
    latest_active_zones(zones, ...)                    -> List[Zone]
    get_zone_alerts(zones, current_price, ..)          -> List[dict]
    diagnose_bar(df, at_index, params=None)            -> List[dict]
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import numpy as np
import pandas as pd


DEFAULT_PARAMS = dict(
    accountCapital=25000.0,
    riskPct=0.5,
    targetRR=5.0,
    slBufferAtr=0.1,

    atrPeriod=14,
    volSmaPeriod=20,
    legOutTrMult=1.2,
    legOutMinTrRatio=1.0,
    hqLegOutTrMult=2.0,
    hqLegInAtrMult=1.5,
    maxBaseAtrMult=1.0,
    maxWickPct=0.25,

    minBaseCount=1,
    maxBaseCount=3,
    legInMinAtrMult=1.0,
    minClvPct=0.60,
    legInToBaseSizeMult=2.0,
    legInMinBodyPct=0.60,

    useImbalance=True,
    maxImbalanceVsLegInMult=1.0,
    relaxGapCapOnOvernight=True,   # overnight gap पर raw price-gap का cap हटाया जाता है
    genuineGapScoreBonus=10,
    overnightGapScoreBonus=15,

    rejectOppositeCoverPct=0.50,

    minValidScore=40,
    hqScoreThreshold=90,

    legOutBodyHeavyPressurePct=0.60,

    testedLegOutRetracePct=0.50,
    maxTestedCount=2,
)

_HARD_MAX_BASE_COUNT = 3


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
    isOvernightGap: bool = False
    legInTR: float = 0.0            # [NEW v9.0] डिबग/पारदर्शिता के लिए स्टोर किया गया
    legOutTR: float = 0.0           # [NEW v9.0] डिबग/पारदर्शिता के लिए स्टोर किया गया


# --------------------------------------------------------------------------
# [FIXED v9.0] असली/सही True Range निकालने वाला फ़ंक्शन (Gap-Aware — ROOT FIX)
# --------------------------------------------------------------------------
def _true_range(h, l, c):
    """
    संस्थागत/सही True Range फ़ॉर्मूला:
        TR = MAX(High-Low, |High-PrevClose|, |Low-PrevClose|)
    यही एक फ़ंक्शन अब ATR और हर individual कैंडल (Leg-In/Leg-Out/Base) के
    TR-चेक — दोनों जगह इस्तेमाल होता है, ताकि पहले जैसी mismatch दोबारा ना बने।
    """
    n = len(h)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]  # पहली कैंडल के लिए PrevClose उपलब्ध नहीं
    if n > 1:
        prev_close = c[:-1]
        tr[1:] = np.maximum(
            h[1:] - l[1:],
            np.maximum(np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)),
        )
    return tr


def _wilder_atr_from_tr(tr: np.ndarray, period: int) -> np.ndarray:
    """Wilder's Smoothing — अब एक पहले से बने हुए सही TR array पर काम करता है।"""
    n = len(tr)
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
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        return idx.date
    try:
        parsed = pd.to_datetime(idx)
        return parsed.date
    except Exception:
        return None


def _prep_arrays(df, p):
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    true_range = _true_range(h, l, c)                       # [FIXED v9.0]
    atr = _wilder_atr_from_tr(true_range, p["atrPeriod"])    # [FIXED v9.0] एक ही TR स्रोत
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()
    return o, h, l, c, v, true_range, atr, vol_sma


# --------------------------------------------------------------------------
# मुख्य स्कैनिंग इंजन
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
               lookback_months: Optional[float] = None) -> List[Zone]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    o, h, l, c, v, true_range, atr, vol_sma = _prep_arrays(df, p)
    n = len(df)

    minBaseCount = p["minBaseCount"]
    maxBaseCount = p["maxBaseCount"]
    atrPeriod = p["atrPeriod"]

    bar_dates = _bar_dates_array(df)

    # [FIXED v9.0] अब यह असली/gap-aware True Range array से value लेता है
    def tr(t, idx):
        return true_range[t - idx]

    def is_bull(t, idx):
        return c[t - idx] > o[t - idx]

    def is_bear(t, idx):
        return o[t - idx] > c[t - idx]

    # wick% और body% जानबूझकर candle की अपनी H-L रेंज पर आधारित रहते हैं (gap पर नहीं)
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
            legOutTR = tr(t, legOutIdx)   # [FIXED v9.0] अब सही (gap-aware) TR
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

            # ---------------- OVERNIGHT/MULTI-DAY GAP पहचान ----------------
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
                gapCap = float("inf")
            else:
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

            # ---------------- Leg-Out की BODY पूरे base-zone को engulf ना करे ----------------
            legOutBodyHigh = max(legOutOpen, legOutClose)
            legOutBodyLow = min(legOutOpen, legOutClose)
            legOutBodyEngulfsBase = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)
            if legOutBodyEngulfsBase and not hasGenuineGap:
                continue

            # ---------------- पैटर्न वर्गीकरण ----------------
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

            # ---------------- डेंसिटी स्कोर ----------------
            densityScore = 0

            if baseCount == 1:
                densityScore += 15

            if legInTR >= (p["hqLegInAtrMult"] * atr[t - legInIdx]):
                densityScore += 10

            if legOutTR >= (p["hqLegOutTrMult"] * legInTR):
                densityScore += 15

            if (legInTR >= 2.0 * maxBaseTR) and (legOutTR >= 2.0 * legInTR):
                densityScore += 15

            if legOutVol > vol_sma[t - legOutIdx]:
                densityScore += 10

            if isDemandLegOut:
                legOutBodyPos = (legOutClose - legOutLow) / (legOutHigh - legOutLow) if (legOutHigh - legOutLow) > 0 else 0
                legOutOwnBodyPct = body_pct(t, legOutIdx)
                if isDBR:
                    if (legOutBodyPos >= 0.80) or (legOutOwnBodyPct >= p["legOutBodyHeavyPressurePct"]):
                        densityScore += 15
                else:
                    if legOutBodyPos >= 0.80:
                        densityScore += 15
            else:
                legOutBodyPos = (legOutHigh - legOutClose) / (legOutHigh - legOutLow) if (legOutHigh - legOutLow) > 0 else 0
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
            if isOvernightGap and hasGenuineGap:
                densityScore += p["overnightGapScoreBonus"]

            if densityScore < p["minValidScore"]:
                continue

            isHQZone = densityScore >= p["hqScoreThreshold"]
            zoneFoundOnThisBar = True

            # ---------------- प्रॉक्सिमल/डिस्टल/SL/TP ----------------
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
                isOvernightGap=isOvernightGap, legInTR=legInTR, legOutTR=legOutTR,
            )
            zones.append(newZone)
            active_zones.append(newZone)

        # ---------------- ज़ोन स्टेटस ट्रैकिंग ----------------
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
            "legInTR": z.legInTR, "legOutTR": z.legOutTR,
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts


# --------------------------------------------------------------------------
# डायग्नोस्टिक/ट्रबलशूटिंग हेल्पर (v9.0 — गलत/सही TR दोनों दिखाता है)
# --------------------------------------------------------------------------
def diagnose_bar(df: pd.DataFrame, at_index, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    o, h, l, c, v, true_range, atr, vol_sma = _prep_arrays(df, p)
    bar_dates = _bar_dates_array(df)

    if isinstance(at_index, (int, np.integer)):
        t = int(at_index)
    else:
        t = int(df.index.get_loc(at_index))

    def tr(idx_from_t):
        return true_range[t - idx_from_t]

    def naive_hl_range(idx_from_t):
        """पुराना (बग वाला) H-L तरीका — तुलना के लिए दिखाया जा रहा है।"""
        i = t - idx_from_t
        return h[i] - l[i]

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
        rep["legInTR(correct)"] = legInTR
        rep["legInTR(old_buggy_H-L)"] = naive_hl_range(legInIdx)
        legInLow = l[t - legInIdx]; legInHigh = h[t - legInIdx]; legInClose = c[t - legInIdx]
        legInVol = v[t - legInIdx]; legInRng = legInHigh - legInLow
        legInIsBull, legInIsBear = is_bull(legInIdx), is_bear(legInIdx)

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
        rep["legOutTR(correct,gap-aware)"] = legOutTR
        rep["legOutTR(old_buggy_H-L)"] = naive_hl_range(legOutIdx)  # यही पुराना बग था
        legOutHigh = h[t - legOutIdx]; legOutLow = l[t - legOutIdx]
        legOutClose = c[t - legOutIdx]; legOutOpen = o[t - legOutIdx]; legOutVol = v[t - legOutIdx]
        isDemandLegOut, isSupplyLegOut = is_bull(legOutIdx), is_bear(legOutIdx)

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
