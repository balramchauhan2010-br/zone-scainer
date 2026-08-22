
# -*- coding: utf-8 -*-
"""
zone_core.py — v8.2 (Advanced D&S Engine with Leg-Out TR Multiplier & Refined Rules)
यह एक एडवांस्ड डिमांड और सप्लाई (D&S) ज़ोन डिटेक्शन इंजन है।
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import numpy as np
import pandas as pd


# सिस्टम के डिफ़ॉल्ट पैरामीटर्स की डिक्शनरी
DEFAULT_PARAMS = dict(
    # --- कैपिटल और रिस्क सेटिंग्स ---
    accountCapital=25000.0,   # कुल खाता कैपिटल ($25,000)
    riskPct=0.5,              # हर ट्रेड पर 0.5% रिस्क
    targetRR=5.0,             # रिस्क-टू-रिवॉर्ड रेशियो (1:5)
    slBufferAtr=0.1,           # स्टॉपलॉस ATR बफर

    # --- एल्गोरिदम और फिल्टर्स ---
    atrPeriod=14,             # ATR इंडिकेटर अवधि
    volSmaPeriod=20,          # एवरेज वॉल्यूम अवधि
    legOutTrMult=1.2,         # Leg-Out कैंडल न्यूनतम True Range मल्टीप्लायर
    hqLegOutAtrMult=2.0,      # हाई क्वालिटी Leg-Out ATR मल्टीप्लायर
    hqLegInAtrMult=1.5,       # हाई क्वालिटी Leg-In ATR मल्टीप्लायर
    maxBaseAtrMult=1.0,       # बेस कैंडल अधिकतम ATR साइज
    maxWickPct=0.25,          # Leg-Out कैंडल में अधिकतम विक % (25%)

    # --- बेस और लेग-इन नियम ---
    minBaseCount=1,           # न्यूनतम बेस कैंडल
    maxBaseCount=3,           # अधिकतम बेस कैंडल
    legInMinAtrMult=1.0,      # Leg-In कैंडल न्यूनतम ATR मल्टीप्लायर
    minClvPct=0.60,           # Leg-In कैंडल न्यूनतम CLV (60%)
    legInToBaseSizeMult=2.0,  # Leg-In कैंडल सबसे बड़े बेस से कम से कम 2x होनी चाहिए

    # --- इमबैलेंस और स्विंग सेटिंग्स ---
    useImbalance=True,
    swingLeftBars=3,
    swingRightBars=3,
)


_HARD_MAX_BASE_COUNT = 3


# ज़ोन डेटा स्ट्रक्चर
@dataclass
class Zone:
    proxVal: float                  # प्रॉक्सिमल लाइन (एंट्री)
    distVal: float                  # डिस्टल लाइन (SL स्तर)
    slVal: float                    # स्टॉपलॉस प्राइस
    tpVal: float                    # टेक प्रॉफिट प्राइस
    isDemand: bool                  # True = Demand, False = Supply
    isHQ: bool                      # High Quality Zone Flag
    densityScore: int               # ज़ोन क्वालिटी स्कोर (0-100)
    patternType: str = ""           # RBR, DBR, DBD, RBD
    zoneCategory: str = ""          # Continuation / Reversal
    state: str = "Fresh"            # Fresh, Tested, Broken
    touchCount: int = 0             # ज़ोन टेस्ट काउंट
    originalDensityScore: int = 0   # शुरुआती स्कोर
    startBarIndex: int = 0          # शुरुआत कैंडल इंडेक्स
    createdBarIndex: int = 0        # निर्माण कैंडल इंडेक्स
    baseCount: int = 0              # बेस कैंडल की संख्या
    timestamp: object = None        # ज़ोन टाइमस्टैम्प
    qty: float = 0.0                # रिस्क के आधार पर पोजीशन साइज
    sweptLiquidity: bool = False    # फ्लैग (पिछली संगतता के लिए)


# Wilder's ATR कैलकुलेशन
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


# लुकबैक महीनों के हिसाब से शुरुआती कैंडल तय करना
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


# --------------------------------------------------------------------------
# मुख्य स्कैनिंग इंजन (Core Scan Function)
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

    # ATR एवं वॉल्यूम SMA (Average Volume)
    atr = _wilder_atr(h, l, c, atrPeriod)
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()

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

    zones: List[Zone] = []
    active_zones: List[Zone] = []
    min_start = max(atrPeriod, maxBaseCount + 2, p["swingLeftBars"] + p["swingRightBars"] + 1, 11)
    record_from_bar = max(min_start, _resolve_start_bar_for_lookback(df, lookback_months))

    # Backward compatibility support for legOutTrMult / legOutAtrMult
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
            if t - (legInIdx + 1) < 0 or t - baseCount < 0:
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

            bullClv = (legInClose - legInLow) / legInRng
            bearClv = (legInHigh - legInClose) / legInRng

            # ---------------- BASE की जाँच ----------------
            allBaseValid = True
            maxBaseTR = 0.0
            maxBaseHigh = -1.0
            minBaseLow = float("inf")
            hasOppositeColorBase = False

            # सभी बेस कैंडल्स में से सबसे बड़ी baseTR और High/Low निकालना
            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    allBaseValid = False
                    break
                bTR = tr(t, b)

                # बेस कैंडल 1x ATR से छोटी होनी चाहिए
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

            # मुख्य नियम: Leg-In कैंडल सबसे बड़ी बेस कैंडल (maxBaseTR) से कम से कम 2x बड़ी होनी चाहिए
            if legInTR < (p["legInToBaseSizeMult"] * maxBaseTR):
                continue

            # Leg-In ATR साइज चेकिंग
            validLegIn = legInTR >= (p["legInMinAtrMult"] * atr[t - legInIdx])
            if not validLegIn:
                continue

            # ---------------- LEG-OUT की जाँच ----------------
            legOutTR = tr(t, legOutIdx)
            legOutHigh = h[t - legOutIdx]
            legOutLow = l[t - legOutIdx]
            legOutClose = c[t - legOutIdx]
            legOutVol = v[t - legOutIdx]

            isDemandLegOut = is_bull(t, legOutIdx)
            isSupplyLegOut = is_bear(t, legOutIdx)
            if not (isDemandLegOut or isSupplyLegOut):
                continue

            # True Range मल्टीप्लायर के आधार पर Explosive चेक
            isLegOutExplosive = legOutTR >= (legOutMult * atr[t - legOutIdx])
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["maxWickPct"]
            passesTRHierarchy = (legOutTR > legInTR) and (legInTR > maxBaseTR)
            passesVolume = legOutVol > legInVol

            # इमबैलेंस चेक
            hasImbalance = True
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasImbalance = (legOutLow > maxBaseHigh) or (legOutClose > legInHigh)
                elif isSupplyLegOut:
                    hasImbalance = (legOutHigh < minBaseLow) or (legOutClose < legInLow)

            # ---------------- पैटर्न क्लासिफिकेशन ----------------
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

            zoneFoundOnThisBar = True

            # ---------------- डेंसिटी स्कोर (Density Score Calculation) ----------------
            densityScore = 0

            # 1. Zone में केवल 1 Base कैंडल हो (+15 अंक)
            if baseCount == 1:
                densityScore += 15

            # 2. Leg-In कैंडल Explosive हो (+10 अंक)
            if legInTR >= (p["hqLegInAtrMult"] * atr[t - legInIdx]):
                densityScore += 10

            # 3. Leg-Out कैंडल Explosive हो (+15 अंक)
            if legOutTR >= (p["hqLegOutAtrMult"] * atr[t - legOutIdx]):
                densityScore += 15

            # 4. Leg-In > 2x Base size और Leg-Out > 2x Leg-In size हो (+15 अंक)
            if (legInTR >= 2.0 * maxBaseTR) and (legOutTR >= 2.0 * legInTR):
                densityScore += 15

            # 5. Leg-Out कैंडल में एवरेज से ज्यादा वॉल्यूम हो (+10 अंक)
            if legOutVol > vol_sma[t - legOutIdx]:
                densityScore += 10

            # 6. Leg-Out का क्लोज़ आधे से ऊपर (Demand) या आधे से नीचे (Supply) हो (+15 अंक)
            if isDemandLegOut:
                legOutBodyPos = (legOutClose - legOutLow) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.50:
                    densityScore += 15
            else:
                legOutBodyPos = (legOutHigh - legOutClose) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.50:
                    densityScore += 15

            # 7. डिमांड ज़ोन में Base कैंडल Red हो या सप्लाई ज़ोन में Base कैंडल Green हो (+10 अंक)
            for b in range(1, baseCount + 1):
                if isDemandLegOut and is_bear(t, b):
                    hasOppositeColorBase = True
                    break
                elif isSupplyLegOut and is_bull(t, b):
                    hasOppositeColorBase = True
                    break
            if hasOppositeColorBase:
                densityScore += 10

            # 8. Fresh Zone (प्राइस ने अभी बेस कैंडल को टच न किया हो) (+10 अंक)
            densityScore += 10

            # 70 से अधिक अंक वाले ज़ोन High Quality माने जाएँगे
            isHQZone = densityScore >= 70

            # ---------------- प्रॉक्सिमल और डिस्टल लाइन्स (एंट्री/SL/TP/Qty) ----------------
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            # रिस्क और पोजीशन साइज़ (Quantity)
            riskAmount = p["accountCapital"] * (p["riskPct"] / 100.0)
            qty = round(riskAmount / riskPerShare, 2) if riskPerShare > 0 else 0.0

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
                timestamp=df.index[t], qty=qty, sweptLiquidity=False,
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
                        if lo_t <= z.proxVal and lo_t > z.distVal:
                            z.state = "Tested"
                            z.touchCount += 1
                        elif lo_t <= z.distVal:
                            z.state = "Broken"
                    else:
                        if hi_t >= z.proxVal and hi_t < z.distVal:
                            z.state = "Tested"
                            z.touchCount += 1
                        elif hi_t >= z.distVal:
                            z.state = "Broken"
                elif z.state == "Tested":
                    if z.isDemand:
                        if lo_t <= z.distVal:
                            z.state = "Broken"
                        elif lo_t <= z.proxVal:
                            z.touchCount += 1
                    else:
                        if hi_t >= z.distVal:
                            z.state = "Broken"
                        elif hi_t >= z.proxVal:
                            z.touchCount += 1

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
            "score": z.densityScore, "touch_count": z.touchCount, "qty": z.qty,
            "swept_liquidity": z.sweptLiquidity,
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts
