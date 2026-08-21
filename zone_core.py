# -*- coding: utf-8 -*-
"""
new.py — Zone Core (Modified rules as per user)
==============================================

NEW/CHANGED VALIDATION RULES (Added on top of existing engine):
---------------------------------------------------------------
1) Leg-In candle must show "pressure" using Directional CLV >= 60%:
   - If Leg-In is Bullish  => Buy pressure:  CLV = (Close-Low)/(High-Low)
   - If Leg-In is Bearish  => Sell pressure: CLV = (High-Close)/(High-Low)
   Requirement: CLV >= 0.60

   This enforces:
   - DBR (Drop-Base-Rally):    Leg-In bearish + sell pressure (CLV>=60%)
   - RBD (Rally-Base-Drop):    Leg-In bullish + buy pressure  (CLV>=60%)
   - RBR (Rally-Base-Rally):   Leg-In bullish + buy pressure  (CLV>=60%)
   - DBD (Drop-Base-Drop):     Leg-In bearish + sell pressure (CLV>=60%)

2) Base candle count must be <= 3 (hard capped).

3) Base candles must be smaller than Leg-In candle (visual rule):
   maxBaseTR < baseTrMaxFracOfLegIn * legInTR
   (default baseTrMaxFracOfLegIn = 1.0 => maxBaseTR < legInTR)

4) Leg-In TR must be greater than Base ATR (average ATR of base candles):
   legInTR > legInTrGtBaseAtrMult * mean(ATR of base candles)
   (default multiplier = 1.0)

5) Leg-Out remains:
   - Correct direction (bull/bear)
   - Explosive (TR >= legOutAtrMult * ATR)
   plus existing filters (wick%, hierarchy, BOS, volume, imbalance, sweep)
   as in your given code.

Public entry points:
    scan_zones(df, params=None, lookback_months=None) -> List[Zone]
    latest_active_zones(zones, ...)                    -> List[Zone]
    get_zone_alerts(zones, current_price, ..)          -> List[dict]
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import numpy as np
import pandas as pd


DEFAULT_PARAMS = dict(
    # --- Capital / position sizing ---
    accountCapital=25000.0,
    riskPct=0.5,
    targetRR=5.0,
    slBufferAtr=0.1,

    # --- Algo & sweep filters ---
    atrPeriod=14,
    legOutAtrMult=1.2,        # Explosive: TR >= 1.2 x ATR
    hqLegOutAtr=2.0,
    maxBaseAtrMult=1.0,       # Base candle max TR (x ATR), non-strict <=
    maxWickPct=0.25,
    useSweepFilter=True,
    useImbalance=True,

    # --- Base & leg-in rules ---
    minBaseCount=1,
    maxBaseCount=3,           # HARD requirement: base candles <= 3

    reqLegInVol=True,
    legInVolMinMult=0.8,
    legInMinAtrMult=0.8,      # keeps previous requirement (optional)

    # --- NEW: Leg-In Pressure (Directional CLV) ---
    enforceLegInCLV=True,
    clvThreshold=0.60,        # 60%

    # --- NEW: Base must be smaller than Leg-In ---
    enforceBaseSmallerThanLegIn=True,
    baseTrMaxFracOfLegIn=1.0,  # require maxBaseTR < 1.0*legInTR

    # --- NEW: Leg-In TR must be > Base ATR (mean ATR of base candles) ---
    enforceLegInTrGtBaseAtr=True,
    legInTrGtBaseAtrMult=1.0,

    # --- Swing / liquidity sweep detection ---
    swingLeftBars=5,
    swingRightBars=5,
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
    qty: int = 0
    sweptLiquidity: bool = False


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


def _last_known_swing(values: np.ndarray, is_high: bool, left: int, right: int) -> np.ndarray:
    n = len(values)
    revealed = np.full(n, np.nan)
    for j in range(left, n - right):
        window = values[j - left: j + right + 1]
        center = values[j]
        if is_high:
            if center == window.max() and np.argmax(window) == left:
                reveal_at = j + right
                if reveal_at < n:
                    revealed[reveal_at] = center
        else:
            if center == window.min() and np.argmin(window) == left:
                reveal_at = j + right
                if reveal_at < n:
                    revealed[reveal_at] = center
    return pd.Series(revealed).ffill().to_numpy()


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
# Core scan
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
              lookback_months: Optional[float] = None) -> List[Zone]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    # HARD CAP base candles <= 3
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

    lastSwingHigh = _last_known_swing(h, True, p["swingLeftBars"], p["swingRightBars"])
    lastSwingLow = _last_known_swing(l, False, p["swingLeftBars"], p["swingRightBars"])

    def tr(t, idx):
        # (Your engine uses High-Low as TR for candle range)
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

    def directional_clv(t, idx):
        """
        Directional CLV in [0..1]
        - bullish pressure: close near high
        - bearish pressure: close near low
        """
        i = t - idx
        rng = h[i] - l[i]
        if rng <= 0:
            return 0.0
        if c[i] > o[i]:  # bullish
            return (c[i] - l[i]) / rng
        if o[i] > c[i]:  # bearish
            return (h[i] - c[i]) / rng
        return 0.0  # doji -> no pressure

    zones: List[Zone] = []
    active_zones: List[Zone] = []

    min_start = max(
        atrPeriod,
        maxBaseCount + 2,
        p["swingLeftBars"] + p["swingRightBars"] + 1,
        11
    )
    record_from_bar = max(min_start, _resolve_start_bar_for_lookback(df, lookback_months))

    for t in range(min_start, n):
        if np.isnan(atr[t]):
            continue

        zoneFoundOnThisBar = False

        for baseCount in range(minBaseCount, maxBaseCount + 1):
            if zoneFoundOnThisBar:
                break

            legOutIdx = 0
            legInIdx = baseCount + 1

            # reqLegInVol needs legInIdx+1 bar
            if t - (legInIdx + 1) < 0 or t - baseCount < 0:
                continue
            if np.isnan(atr[t - legInIdx]) or np.isnan(atr[t]):
                continue

            # ---------------- BASE VALIDATION ----------------
            allBaseValid = True
            maxBaseTR = 0.0
            maxBaseHigh = -1.0
            minBaseLow = float("inf")
            baseAtrVals = []

            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    allBaseValid = False
                    break

                bTR = tr(t, b)
                maxBaseTR = max(maxBaseTR, bTR)

                # Base candle must be <= maxBaseAtrMult * ATR(base candle)
                if bTR > (p["maxBaseAtrMult"] * atr[t - b]):
                    allBaseValid = False

                maxBaseHigh = max(maxBaseHigh, h[t - b])
                minBaseLow = min(minBaseLow, l[t - b])

                baseAtrVals.append(float(atr[t - b]))

            if not allBaseValid:
                continue

            baseAtrMean = float(np.mean(baseAtrVals)) if baseAtrVals else float("nan")
            if np.isnan(baseAtrMean) or baseAtrMean <= 0:
                continue

            # ---------------- LEG-IN VALIDATION ----------------
            legInTR = tr(t, legInIdx)
            legInLow = l[t - legInIdx]
            legInHigh = h[t - legInIdx]
            legInVol = v[t - legInIdx]

            legInIsBull = is_bull(t, legInIdx)
            legInIsBear = is_bear(t, legInIdx)
            if not (legInIsBull or legInIsBear):
                continue

            # NEW: base must be smaller than leg-in (visual)
            if p["enforceBaseSmallerThanLegIn"]:
                if not (maxBaseTR < (p["baseTrMaxFracOfLegIn"] * legInTR)):
                    continue

            # NEW: Leg-In TR > Base ATR(mean)
            if p["enforceLegInTrGtBaseAtr"]:
                if not (legInTR > (p["legInTrGtBaseAtrMult"] * baseAtrMean)):
                    continue

            # NEW: Directional CLV >= 60%
            if p["enforceLegInCLV"]:
                legInCLV = directional_clv(t, legInIdx)
                if legInCLV < p["clvThreshold"]:
                    continue

            validLegIn = True
            if p["reqLegInVol"]:
                prevVol = v[t - (legInIdx + 1)]
                validLegIn = (legInVol >= p["legInVolMinMult"] * prevVol) and \
                             (legInTR >= p["legInMinAtrMult"] * atr[t - legInIdx])
            if not validLegIn:
                continue

            # ---------------- LEG-OUT VALIDATION ----------------
            legOutTR = tr(t, legOutIdx)
            legOutHigh = h[t - legOutIdx]
            legOutLow = l[t - legOutIdx]
            legOutClose = c[t - legOutIdx]
            legOutVol = v[t - legOutIdx]

            isDemandLegOut = is_bull(t, legOutIdx)
            isSupplyLegOut = is_bear(t, legOutIdx)
            if not (isDemandLegOut or isSupplyLegOut):
                continue

            # Leg-Out must be explosive
            isLegOutExplosive = legOutTR >= (p["legOutAtrMult"] * atr[t - legOutIdx])

            # Keep existing wick filter + hierarchy + volume etc.
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["maxWickPct"]
            passesTRHierarchy = (legOutTR > legInTR) and (legInTR > maxBaseTR)
            passesVolume = legOutVol > legInVol

            # BOS (close-based)
            hasBOS = False
            if isDemandLegOut:
                hasBOS = legOutClose > max(legInHigh, maxBaseHigh)
            elif isSupplyLegOut:
                hasBOS = legOutClose < min(legInLow, minBaseLow)

            # Imbalance filter
            hasImbalance = True
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasImbalance = (legOutLow > maxBaseHigh) or (legOutClose > legInHigh)
                elif isSupplyLegOut:
                    hasImbalance = (legOutHigh < minBaseLow) or (legOutClose < legInLow)

            # Liquidity sweep filter
            swingHighAtT = lastSwingHigh[t]
            swingLowAtT = lastSwingLow[t]
            sweptLiquidity = False
            if isDemandLegOut and not np.isnan(swingLowAtT):
                sweptLiquidity = (minBaseLow < swingLowAtT) or (legInLow < swingLowAtT)
            elif isSupplyLegOut and not np.isnan(swingHighAtT):
                sweptLiquidity = (maxBaseHigh > swingHighAtT) or (legInHigh > swingHighAtT)
            passesSweepCheck = sweptLiquidity if p["useSweepFilter"] else True

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
                and hasImbalance
                and passesSweepCheck
            )

            if not isValid:
                continue

            zoneFoundOnThisBar = True

            # ---------------- DENSITY SCORE ----------------
            densityScore = 25
            if legOutTR >= p["hqLegOutAtr"] * atr[t - legOutIdx]:
                densityScore += 25
            if sweptLiquidity:
                densityScore += 25
            if baseCount <= 2 and maxBaseTR <= 0.7 * atr[t - 1]:
                densityScore += 25
            isHQZone = densityScore >= 75

            # ---------------- PROXIMAL / DISTAL ----------------
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            riskAmount = p["accountCapital"] * (p["riskPct"] / 100.0)
            qty = int(riskAmount // riskPerShare) if riskPerShare > 0 else 0

            # ---------------- DUPLICATE CHECK ----------------
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
                timestamp=df.index[t], qty=qty, sweptLiquidity=sweptLiquidity,
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
