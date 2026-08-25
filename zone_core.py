# -*- coding: utf-8 -*-
"""
zone_core.py — v9.3  (Pine Script v6 parity — "जैसा Pine स्कैन करता है वैसा ही")
(v9.2 पर आधारित — सिर्फ़ वही बदलाव जो Pine Script v6 से match करने के लिए ज़रूरी थे)

=== v9.2 से v9.3 में क्या बदला (सिर्फ़ 4 बदलाव — Pine v6 से exact match) ===

Pine Script (TradingView) और zone_core.py v9.2 की line-by-line तुलना में 4 जगह
लॉजिक अलग निकला। यही 4 अंतर वजह थे कि Pine ज़्यादा zones scan कर पा रहा था
और Python नहीं। अब दोनों identical हैं:

(1) [GENUINE GAP — Pine की वापस v9.1-style definition]
    Pine : Demand  hasGenuineGap = legOutLow  > maxBaseHigh
           Supply  hasGenuineGap = legOutHigh < minBaseLow
           Demand  gapSize       = max(0, legOutLow  - maxBaseHigh)
           Supply  gapSize       = max(0, minBaseLow - legOutHigh)
    (v9.2 का "legOutOpen vs baseCloseAdjacent" वाला नया फ़ॉर्मूला हटाया गया)

(2) [GAP-SIZE CAP पूरी तरह हटाया गया]
    Pine में hasImbalance = gapCond (सिर्फ़ gap condition) है — कोई gap-size
    cap नहीं, कोई overnight-relax नहीं।
    v9.2 में यह था: hasImbalance = gapCond AND (gapSize <= gapCap)
    अब (v9.3): hasImbalance = gapCond
    इसलिए v9.2 कुछ zones को गलत तरीके से reject कर रहा था जो Pine accept
    करता था। (maxImbalanceVsLegInMult / relaxGapCapOnOvernight params अब भी
    DEFAULT_PARAMS में हैं लेकिन Pine की तरह logic में use नहीं होते।)

(3) [SINGLE-BASE MULTIPLIER 1.2 → 1.5]
    Pine : effectiveBaseSizeMult = (bCount == 1) ? 1.5 : legInToBaseSizeMult
    v9.2 : legInToBaseSizeMultSingleBase = 1.2
    v9.3 : legInToBaseSizeMultSingleBase = 1.5   (Pine parity)

(4) [FRESH→TESTED ट्रिगर वापस legOutMidLevel]
    Pine : Fresh→Tested = legOutMidLevel टच (और Tested में touchCount भी
           legOutMidLevel पर ही बढ़ता है)
    v9.2 : Fresh→Tested = proxVal टच
    v9.3 : वापस legOutMidLevel (Pine parity)

--------------------------------------------------------------------------
बाकी सब कुछ (v9.0/v9.1/v9.2 से) पूरी तरह यथावत:
  - Gap-Aware True Range (हर जगह) — Pine के true_range()/tr(idx) जैसा
  - Wilder ATR (ta.rma जैसा)
  - Leg-In: Body%>=60%, opposite-color-overlap-reject, TR>=ATR, TR>=Mult*MaxBaseTR
  - Base: TR<=ATR हर candle पर
  - Leg-Out: Explosive(>=1.2xATR), Wick%<=30%, TR-Hierarchy, Volume-check,
             Body-engulf-check (genuine gap हो तो exempt)
  - Scoring: <40 invalid, >=90 HQ, overnight+genuine-gap बोनस
  - डुप्लीकेट फिल्टर, state-machine की Broken→distVal लॉजिक
  - v9.1 की highlight-tagging features (reformedAfterBreak, MTF confluence)
--------------------------------------------------------------------------

नोट: Zone dataclass में hasGenuineGap/gapSize store किए जाते हैं — Pine में ये
Zone में store नहीं होते (सिर्फ़ local variable हैं), लेकिन Python में analysis/
debugging के लिए रखे गए हैं। इससे scan का नतीजा नहीं बदलता।

Public entry points:
    scan_zones(df, params=None, lookback_months=None)          -> List[Zone]
    latest_active_zones(zones, ...)                             -> List[Zone]
    get_zone_alerts(zones, current_price, ..)                   -> List[dict]
    diagnose_bar(df, at_index, params=None)                     -> List[dict]
    flag_multi_timeframe_confluence(zones_by_tf, tf_order)      -> None (in-place)
    zone_highlight_tags(zone)                                   -> List[str]
"""
from dataclasses import dataclass, field
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
    maxWickPct=0.30,   # [यथावत] Leg-Out कैंडल पर लागू
    minBaseCount=1,
    maxBaseCount=3,
    legInMinAtrMult=1.0,
    minClvPct=0.60,
    legInToBaseSizeMult=2.0,             # baseCount == 2 या 3 के लिए (यथावत)
    legInToBaseSizeMultSingleBase=1.5,   # [CHANGED v9.3] 1.2 → 1.5 (Pine: 1.5)
    legInMinBodyPct=0.60,
    useImbalance=True,
    maxImbalanceVsLegInMult=1.0,   # [Pine parity] Pine में defined-but-unused; अब logic में use नहीं
    relaxGapCapOnOvernight=True,   # [Pine parity] Pine में defined-but-unused; अब logic में use नहीं
    genuineGapScoreBonus=10,
    overnightGapScoreBonus=15,
    rejectOppositeCoverPct=0.50,
    minValidScore=40,
    hqScoreThreshold=90,
    legOutBodyHeavyPressurePct=0.60,
    testedLegOutRetracePct=0.50,   # legOutMidLevel के लिए — Pine parity के तहत state-logic में फिर से use होता है
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
    legOutMidLevel: float = 0.0   # Pine parity: state-logic में फिर से use होता है
    isOvernightGap: bool = False
    legInTR: float = 0.0
    legOutTR: float = 0.0
    hasGenuineGap: bool = False   # [Pine में local var — Python में analysis के लिए store]
    gapSize: float = 0.0          # [Pine में local var — Python में analysis के लिए store]
    reformedAfterBreak: bool = False
    isMTFConfluence: bool = False
    isNestedInBiggerTF: bool = False
    confluenceTFs: list = field(default_factory=list)


# --------------------------------------------------------------------------
# सही True Range (Gap-Aware) — यथावत (Pine true_range()/tr(idx) जैसा)
# --------------------------------------------------------------------------
def _true_range(h, l, c):
    n = len(h)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    if n > 1:
        prev_close = c[:-1]
        tr[1:] = np.maximum(
            h[1:] - l[1:],
            np.maximum(np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)),
        )
    return tr


def _wilder_atr_from_tr(tr: np.ndarray, period: int) -> np.ndarray:
    # Pine ta.rma() = Wilder smoothing, बिल्कुल वही
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
    # (Python-only extra feature — Pine में नहीं है, scan logic पर असर नहीं)
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
    true_range = _true_range(h, l, c)
    atr = _wilder_atr_from_tr(true_range, p["atrPeriod"])
    # [Pine parity] ta.sma(volume, volSmaPeriod) पूरे window के बिना na देता है —
    # इसलिए plain rolling (बिना min_periods) रखा गया, ताकि शुरुआती bars पर
    # vol_sma NaN हो और legOutVol > NaN False हो (बिल्कुल Pine जैसा)
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"]).mean().to_numpy()
    return o, h, l, c, v, true_range, atr, vol_sma


def _zone_range(z: "Zone"):
    return min(z.proxVal, z.distVal), max(z.proxVal, z.distVal)


def _ranges_overlap(a_lo, a_hi, b_lo, b_hi) -> bool:
    return max(a_lo, b_lo) <= min(a_hi, b_hi)


def _ranges_nested(inner_lo, inner_hi, outer_lo, outer_hi) -> bool:
    return outer_lo <= inner_lo and inner_hi <= outer_hi


# --------------------------------------------------------------------------
# मुख्य स्कैनिंग इंजन (Pine v6 parity)
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
               lookback_months: Optional[float] = None) -> List[Zone]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    # [Pine parity] min/max baseCount का clamp Pine के order में:
    #   minBaseCount = max(1, min(minBaseCountInput, maxBaseCountInput))
    #   maxBaseCount = min(maxBaseCountInput, HARD_MAX_BASE_COUNT)
    raw_max_base = int(p["maxBaseCount"])
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), raw_max_base))
    p["maxBaseCount"] = min(raw_max_base, _HARD_MAX_BASE_COUNT)

    o, h, l, c, v, true_range, atr, vol_sma = _prep_arrays(df, p)
    n = len(df)
    minBaseCount = p["minBaseCount"]
    maxBaseCount = p["maxBaseCount"]
    atrPeriod = p["atrPeriod"]
    bar_dates = _bar_dates_array(df)

    def tr(t, idx):
        return true_range[t - idx]

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
    legOutMult = p["legOutTrMult"]

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

            # [Pine parity] baseCount==1 पर 1.5 multiplier, बाकी पर legInToBaseSizeMult
            effectiveLegInToBaseMult = (
                p["legInToBaseSizeMultSingleBase"] if baseCount == 1
                else p["legInToBaseSizeMult"]
            )
            if legInTR < (effectiveLegInToBaseMult * maxBaseTR):
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

            # ---------------- OVERNIGHT/MULTI-DAY GAP पहचान (Pine isOvernightGap() जैसा) ----------------
            isOvernightGap = False
            if bar_dates is not None:
                try:
                    isOvernightGap = bar_dates[t] != bar_dates[t - 1]
                except Exception:
                    isOvernightGap = False

            # ---------------- [CHANGED v9.3] प्राइस इमबैलेंस/GAP चेकिंग (Pine parity) ----------------
            # hasImbalance = gapCond (कोई gap-size cap नहीं — v9.2 का cap हटाया गया)
            hasImbalance = True
            hasGenuineGap = False
            gapSize = 0.0
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasGenuineGap = legOutLow > maxBaseHigh
                    gapCond = hasGenuineGap or (legOutClose > legInHigh)
                    gapSize = max(0.0, legOutLow - maxBaseHigh)
                    hasImbalance = gapCond
                elif isSupplyLegOut:
                    hasGenuineGap = legOutHigh < minBaseLow
                    gapCond = hasGenuineGap or (legOutClose < legInLow)
                    gapSize = max(0.0, minBaseLow - legOutHigh)
                    hasImbalance = gapCond

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

            # ---------------- डेंसिटी स्कोर (यथावत) ----------------
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

            # ---------------- प्रॉक्सिमल/डिस्टल/SL/TP (यथावत) ----------------
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh
            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])
            if isDemandLegOut:
                legOutMidLevel = legOutHigh - p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)
            else:
                legOutMidLevel = legOutLow + p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)

            # ---------------- डुप्लीकेट ज़ोन फिल्टर (यथावत — Pine active_zones loop जैसा) ----------------
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

            # ---------------- "Re-formed after Break" हाइलाइट (v9.1, यथावत) ----------------
            zoneLow, zoneHigh = min(proxVal, distVal), max(proxVal, distVal)
            reformedAfterBreak = False
            checkedBroken = 0
            for oldZ in reversed(zones):
                if oldZ.state != "Broken" or oldZ.isDemand != isDemandLegOut:
                    continue
                oldLow, oldHigh = min(oldZ.proxVal, oldZ.distVal), max(oldZ.proxVal, oldZ.distVal)
                if _ranges_overlap(zoneLow, zoneHigh, oldLow, oldHigh):
                    reformedAfterBreak = True
                    break
                checkedBroken += 1
                if checkedBroken >= 20:
                    break

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
                hasGenuineGap=hasGenuineGap, gapSize=gapSize,
                reformedAfterBreak=reformedAfterBreak,
            )
            zones.append(newZone)
            active_zones.append(newZone)

        # ---------------- [CHANGED v9.3] ज़ोन स्टेटस ट्रैकिंग (Pine parity) ----------------
        # Fresh -> Tested फिर से "legOutMidLevel" टच होने पर (Pine जैसा)
        # Tested -> Broken अब भी "distVal" टूटने पर (यथावत)
        if active_zones:
            lo_t, hi_t = l[t], h[t]
            still_active = []
            for z in active_zones:
                if z.state == "Fresh":
                    if z.isDemand:
                        if lo_t <= z.distVal:
                            z.state = "Broken"
                        elif lo_t <= z.legOutMidLevel:          # [CHANGED v9.3] legOutMidLevel टच
                            z.state = "Tested"
                            z.touchCount += 1
                    else:
                        if hi_t >= z.distVal:
                            z.state = "Broken"
                        elif hi_t >= z.legOutMidLevel:           # [CHANGED v9.3] legOutMidLevel टच
                            z.state = "Tested"
                            z.touchCount += 1
                elif z.state == "Tested":
                    if z.isDemand:
                        if lo_t <= z.distVal:
                            z.state = "Broken"
                        elif lo_t <= z.legOutMidLevel:           # [CHANGED v9.3]
                            z.touchCount += 1
                    else:
                        if hi_t >= z.distVal:
                            z.state = "Broken"
                        elif hi_t >= z.legOutMidLevel:            # [CHANGED v9.3]
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
            "reformed_after_break": z.reformedAfterBreak,
            "is_mtf_confluence": z.isMTFConfluence,
            "is_nested_in_bigger_tf": z.isNestedInBiggerTF,
            "confluence_tfs": z.confluenceTFs,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts


def flag_multi_timeframe_confluence(
    zones_by_timeframe: Dict[str, List[Zone]],
    tf_order_small_to_large: List[str],
    only_active: bool = True,
) -> None:
    for tf_zones in zones_by_timeframe.values():
        for z in tf_zones:
            z.isMTFConfluence = False
            z.isNestedInBiggerTF = False
            z.confluenceTFs = []

    for i, small_tf in enumerate(tf_order_small_to_large):
        small_zones = zones_by_timeframe.get(small_tf, [])
        for z_small in small_zones:
            if only_active and z_small.state not in ("Fresh", "Tested"):
                continue
            s_lo, s_hi = _zone_range(z_small)
            for big_tf in tf_order_small_to_large[i + 1:]:
                big_zones = zones_by_timeframe.get(big_tf, [])
                for z_big in big_zones:
                    if only_active and z_big.state not in ("Fresh", "Tested"):
                        continue
                    if z_big.isDemand != z_small.isDemand:
                        continue
                    b_lo, b_hi = _zone_range(z_big)
                    if _ranges_overlap(s_lo, s_hi, b_lo, b_hi):
                        z_small.isMTFConfluence = True
                        if big_tf not in z_small.confluenceTFs:
                            z_small.confluenceTFs.append(big_tf)
                        if _ranges_nested(s_lo, s_hi, b_lo, b_hi):
                            z_small.isNestedInBiggerTF = True


def zone_highlight_tags(z: Zone) -> List[str]:
    tags = []
    if z.isHQ:
        tags.append("⭐ HQ")
    if z.isOvernightGap:
        tags.append("🌙 Overnight-Gap")
    if z.reformedAfterBreak:
        tags.append("🔁 Re-formed after Break")
    if z.isNestedInBiggerTF:
        tags.append(f"📦 Nested in {'/'.join(z.confluenceTFs)}")
    elif z.isMTFConfluence:
        tags.append(f"🧩 MTF Confluence ({'/'.join(z.confluenceTFs)})")
    return tags


# --------------------------------------------------------------------------
# डायग्नोस्टिक/ट्रबलशूटिंग हेल्पर (v9.3 — सभी बदलाव सिंक किए गए)
# --------------------------------------------------------------------------
def diagnose_bar(df: pd.DataFrame, at_index, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    raw_max_base = int(p["maxBaseCount"])
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), raw_max_base))
    p["maxBaseCount"] = min(raw_max_base, _HARD_MAX_BASE_COUNT)
    o, h, l, c, v, true_range, atr, vol_sma = _prep_arrays(df, p)
    bar_dates = _bar_dates_array(df)
    if isinstance(at_index, (int, np.integer)):
        t = int(at_index)
    else:
        t = int(df.index.get_loc(at_index))

    def tr(idx_from_t):
        return true_range[t - idx_from_t]

    def naive_hl_range(idx_from_t):
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

    legOutMult = p["legOutTrMult"]
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

        # [Pine parity] baseCount==1 पर 1.5, बाकी पर legInToBaseSizeMult
        effMult = p["legInToBaseSizeMultSingleBase"] if baseCount == 1 else p["legInToBaseSizeMult"]
        rep["legInToBaseMult_used"] = effMult
        rep["legIn_gte_2xBase"] = legInTR >= (effMult * maxBaseTR) if maxBaseTR else False

        legOutTR = tr(legOutIdx)
        rep["legOutTR(correct,gap-aware)"] = legOutTR
        rep["legOutTR(old_buggy_H-L)"] = naive_hl_range(legOutIdx)
        legOutHigh = h[t - legOutIdx]; legOutLow = l[t - legOutIdx]
        legOutClose = c[t - legOutIdx]; legOutOpen = o[t - legOutIdx]; legOutVol = v[t - legOutIdx]
        isDemandLegOut, isSupplyLegOut = is_bull(legOutIdx), is_bear(legOutIdx)
        rep["legOutATR"] = atr[t - legOutIdx]
        rep["legOut_explosive(>=1.2xATR)"] = legOutTR >= (legOutMult * atr[t - legOutIdx])
        rep["legOut_wickPct"] = wick_pct(legOutIdx)
        rep["legOut_wick_ok"] = rep["legOut_wickPct"] <= p["maxWickPct"]
        rep["TR_hierarchy_ok(legOut>=legIn>base)"] = (legOutTR >= p["legOutMinTrRatio"] * legInTR) and (legInTR > maxBaseTR)
        rep["volume_ok(legOut>legIn)"] = legOutVol > legInVol
        isOvernightGap = False
        if bar_dates is not None:
            try:
                isOvernightGap = bar_dates[t] != bar_dates[t - 1]
            except Exception:
                isOvernightGap = False
        rep["isOvernightGap"] = isOvernightGap

        # [CHANGED v9.3] Pine parity genuine-gap definition — diagnose_bar में भी सिंक
        hasGenuineGap = False; gapSize = 0.0; hasImbalance = True
        if p["useImbalance"]:
            if isDemandLegOut:
                hasGenuineGap = legOutLow > maxBaseHigh
                gapCond = hasGenuineGap or (legOutClose > legInHigh)
                gapSize = max(0.0, legOutLow - maxBaseHigh)
                hasImbalance = gapCond
            elif isSupplyLegOut:
                hasGenuineGap = legOutHigh < minBaseLow
                gapCond = hasGenuineGap or (legOutClose < legInLow)
                gapSize = max(0.0, minBaseLow - legOutHigh)
                hasImbalance = gapCond
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
            and rep["legOut_explosive(>=1.2xATR)"] and rep["legOut_wick_ok"]
            and rep["TR_hierarchy_ok(legOut>=legIn>base)"] and rep["volume_ok(legOut>legIn)"]
            and rep["imbalance_ok"] and rep["engulf_ok"]
        )
        reports.append(rep)
    return reports
