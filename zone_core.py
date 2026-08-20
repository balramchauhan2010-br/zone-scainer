# -*- coding: utf-8 -*-
"""
zone_core.py — v4 (STRICT RULE-BASED: LEG-IN / BASE / LEG-OUT)
==========================================================================
DBR (Demand/Reversal) | RBR (Demand/Continuation) |
RBD (Supply/Reversal) | DBD (Supply/Continuation)

हर zone: patternType ("DBR"/"RBR"/"RBD"/"DBD"), zoneCategory ("Reversal"/"Continuation")

WHAT CHANGED vs v3
-------------------
  - Leg-Out max wick %:      25%  ->  30%   (legOutMaxWickPct)
  - Leg-In min mult of base: 2.0x ->  1.50x (legInMinMultOfBase)
  - Removed the redundant `legIn_hierarchy_ok` (TR > MaxBaseTR) check -
    it was always implied by legIn_mult_ok (>=1.5x already means >1x),
    so it never changed results; dropping it is a no-op cleanup, not a
    rule change.

------------------------------------------------------------------
LEG IN VALIDATION
------------------------------------------------------------------
  Direction: Pattern के अनुसार Bullish/Bearish
  CLV (Close Location Value) >= 60%
       Bullish: (Close - Low) / (High - Low) >= 0.60
       Bearish: (High - Close) / (High - Low) >= 0.60
  True Range >= 0.8 x ATR
  TR >= 1.50 x Max Base TR (legInMinMultOfBase) - this alone also
     guarantees TR > Max Base TR, so no separate hierarchy check needed

------------------------------------------------------------------
BASE VALIDATION (1-3 Candles)
------------------------------------------------------------------
  Count: minBaseCount=1 to maxBaseCount=3
  Each Candle: TR <= 1.0 x ATR (maxBaseAtrMult)

------------------------------------------------------------------
LEG OUT VALIDATION
------------------------------------------------------------------
  Explosive: TR >= 1.2 x ATR (legOutAtrMult)
  HQ Threshold: TR >= 2.0 x ATR -> +25 Density Score
  Wick % <= 30% (legOutMaxWickPct) - Strong Close
  TR Hierarchy: LegOut > LegIn > MaxBaseTR
  BOS (Break of Structure):
       Demand: Close > Max(LegInHigh, MaxBaseHigh)
       Supply: Close < Min(LegInLow, MinBaseLow)
       Demand: Low > MaxBaseHigh OR Close > LegInHigh
       Supply: High < MinBaseLow OR Close < LegInLow
  Volume > Leg In Volume

Public entry points:
    scan_zones(df, params=None)              -> List[Zone]
    latest_active_zones(zones, ...)           -> List[Zone]
    get_zone_alerts(zones, current_price, ..) -> List[dict]
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import numpy as np
import pandas as pd


DEFAULT_PARAMS = dict(
    # --- General ---
    targetRR=5.0,
    slBufferAtr=0.1,
    atrPeriod=14,

    # --- Base rules ---
    minBaseCount=1,
    maxBaseCount=3,
    maxBaseAtrMult=1.0,        # each base candle: TR <= 1.0 x ATR

    # --- Leg-In rules ---
    legInMinAtrMult=0.8,       # TR >= 0.8 x ATR
    legInMinMultOfBase=1.50,   # TR >= 1.50 x MaxBaseTR  (was 2.0)
    legInMinClvPct=0.60,       # CLV >= 60%

    # --- Leg-Out rules ---
    legOutAtrMult=1.2,         # Explosive: TR >= 1.2 x ATR
    hqLegOutAtr=2.0,           # HQ Threshold: TR >= 2.0 x ATR
    legOutMaxWickPct=0.30,     # Wick % <= 30%  (was 0.25)

    # --- Proximal/Distal & risk ---
    legInInclusionFactor=0.35,
    legacyProximalDistal=False,
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
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


def _clv_bullish(o, h, l, c):
    rng = h - l
    return 0.0 if rng <= 0 else (c - l) / rng


def _clv_bearish(o, h, l, c):
    rng = h - l
    return 0.0 if rng <= 0 else (h - c) / rng


# --------------------------------------------------------------------------
# Core scan
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None) -> List[Zone]:
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
    legacy = p["legacyProximalDistal"]
    legInInclusionFactor = p["legInInclusionFactor"]

    atr = _wilder_atr(h, l, c, atrPeriod)

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
    min_start = max(atrPeriod, maxBaseCount + 2, 11)

    for t in range(min_start, n):
        if np.isnan(atr[t]):
            continue

        zoneFoundOnThisBar = False

        for baseCount in range(minBaseCount, maxBaseCount + 1):
            if zoneFoundOnThisBar:
                break

            legOutIdx = 0
            legInIdx = baseCount + 1
            if t - legInIdx < 0 or t - baseCount < 0:
                continue
            if np.isnan(atr[t - legInIdx]):
                continue

            # ---------------- BASE VALIDATION ----------------
            allBaseValid = True
            maxBaseTR = 0.0
            maxBaseHigh = -1.0
            minBaseLow = float("inf")
            baseBodyHighMax = -1.0
            baseBodyLowMin = float("inf")
            base_ok = True

            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    base_ok = False
                    break
                bTR = tr(t, b)
                if bTR > (p["maxBaseAtrMult"] * atr[t - b]):
                    allBaseValid = False
                if bTR > maxBaseTR:
                    maxBaseTR = bTR
                if h[t - b] > maxBaseHigh:
                    maxBaseHigh = h[t - b]
                if l[t - b] < minBaseLow:
                    minBaseLow = l[t - b]
                bodyHigh = max(o[t - b], c[t - b])
                bodyLow = min(o[t - b], c[t - b])
                if bodyHigh > baseBodyHighMax:
                    baseBodyHighMax = bodyHigh
                if bodyLow < baseBodyLowMin:
                    baseBodyLowMin = bodyLow

            if not base_ok or not allBaseValid or maxBaseTR <= 0:
                continue

            # ---------------- LEG IN VALIDATION ----------------
            legInTR = tr(t, legInIdx)
            legInLow = l[t - legInIdx]
            legInHigh = h[t - legInIdx]
            legInOpen = o[t - legInIdx]
            legInClose = c[t - legInIdx]

            legInIsBull = is_bull(t, legInIdx)
            legInIsBear = is_bear(t, legInIdx)

            if not (legInIsBull or legInIsBear):
                continue

            # CLV >= 60%
            if legInIsBull:
                clv = _clv_bullish(legInOpen, legInHigh, legInLow, legInClose)
            else:
                clv = _clv_bearish(legInOpen, legInHigh, legInLow, legInClose)
            clv_ok = clv >= p["legInMinClvPct"]

            # TR >= 0.8 x ATR
            legIn_tr_atr_ok = legInTR >= (p["legInMinAtrMult"] * atr[t - legInIdx])

            # TR >= 1.50 x Max Base TR (this alone implies TR > MaxBaseTR too,
            # so the old separate ">" hierarchy check is redundant and removed)
            legIn_mult_ok = legInTR >= (p["legInMinMultOfBase"] * maxBaseTR)

            validLegIn = clv_ok and legIn_tr_atr_ok and legIn_mult_ok
            if not validLegIn:
                continue

            # ---------------- LEG OUT VALIDATION ----------------
            legOutTR = tr(t, legOutIdx)
            legOutHigh = h[t - legOutIdx]
            legOutLow = l[t - legOutIdx]
            legOutClose = c[t - legOutIdx]

            isDemandLegOut = is_bull(t, legOutIdx)
            isSupplyLegOut = is_bear(t, legOutIdx)
            if not (isDemandLegOut or isSupplyLegOut):
                continue

            # Explosive: TR >= 1.2 x ATR
            isLegOutExplosive = legOutTR >= (p["legOutAtrMult"] * atr[t - legOutIdx])

            # HQ Threshold: TR >= 2.0 x ATR
            isHQCandidate = legOutTR >= (p["hqLegOutAtr"] * atr[t - legOutIdx])

            # Wick % <= 30%
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["legOutMaxWickPct"]

            # TR Hierarchy: LegOut > LegIn > MaxBaseTR
            passesTRHierarchy = (legOutTR > legInTR) and (legInTR > maxBaseTR)

            # BOS (Break of Structure)
            hasBOS = False
            if isDemandLegOut:
                bos_strict = legOutClose > max(legInHigh, maxBaseHigh)
                bos_loose = (legOutLow > maxBaseHigh) or (legOutClose > legInHigh)
                hasBOS = bos_strict or bos_loose
            elif isSupplyLegOut:
                bos_strict = legOutClose < min(legInLow, minBaseLow)
                bos_loose = (legOutHigh < minBaseLow) or (legOutClose < legInLow)
                hasBOS = bos_strict or bos_loose

            # Volume > Leg In Volume
            passesVolume = v[t - legOutIdx] > v[t - legInIdx]

            # ---------------- PATTERN CLASSIFICATION ----------------
            isRBR = legInIsBull and isDemandLegOut
            isDBR = legInIsBear and isDemandLegOut
            isDBD = legInIsBear and isSupplyLegOut
            isRBD = legInIsBull and isSupplyLegOut

            isValid = (
                (isRBR or isDBR or isDBD or isRBD)
                and isLegOutExplosive
                and isLegOutWickValid
                and passesTRHierarchy
                and hasBOS
                and passesVolume
            )

            if not isValid:
                continue

            zoneFoundOnThisBar = True

            # ---------------- DENSITY SCORE ----------------
            densityScore = 50
            if isHQCandidate:
                densityScore += 25
            isHQZone = densityScore >= 75

            # ---------------- PROXIMAL / DISTAL ----------------
            if legacy:
                proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
                distVal = minBaseLow if isDemandLegOut else maxBaseHigh
            else:
                if isDemandLegOut:
                    proxVal = baseBodyHighMax
                    if isDBR:
                        extra = max(0.0, minBaseLow - legInLow)
                        distVal = minBaseLow - extra * legInInclusionFactor
                    else:
                        distVal = minBaseLow
                else:
                    proxVal = baseBodyLowMin
                    if isRBD:
                        extra = max(0.0, legInHigh - maxBaseHigh)
                        distVal = maxBaseHigh + extra * legInInclusionFactor
                    else:
                        distVal = maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            # ---------------- DUPLICATE CHECK ----------------
            isDuplicate = False
            for i in range(0, min(len(zones), 11)):
                checkZ = zones[len(zones) - 1 - i]
                if checkZ.isDemand == isDemandLegOut and abs(checkZ.proxVal - proxVal) < (atr[t] * 0.25):
                    isDuplicate = True
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
            )
            zones.append(newZone)
            active_zones.append(newZone)

        # ---------------- ZONE STATE TRACKING ----------------
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

    return zones


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
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts
