# -*- coding: utf-8 -*-

"""
zone_core.py — v8.3 (Advanced D&S Engine with Leg-Out TR Multiplier, Gap Check & Pre-LegIn Rule)
Advanced Demand and Supply (D&S) Zone Detection Engine.
"""

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import numpy as np
import pandas as pd


# System Default Parameters
DEFAULT_PARAMS = dict(
    # --- Capital & Risk Settings ---
    accountCapital=25000.0,   # Total account capital ($25,000)
    riskPct=0.5,              # Risk percentage per trade (0.5%)
    targetRR=5.0,             # Target Risk-to-Reward ratio (1:5)
    slBufferAtr=0.1,           # Stop loss extra ATR buffer (0.1x ATR)

    # --- Algorithm & Filters ---
    atrPeriod=14,             # ATR period (14 candles)
    volSmaPeriod=20,          # Average Volume SMA period (20 candles)
    legOutTrMult=1.2,         # Leg-Out candle minimum TR multiplier (1.2x ATR)
    hqLegOutAtrMult=2.0,      # High-Quality Leg-Out candle ATR multiplier (2.0x ATR)
    hqLegInAtrMult=1.5,       # High-Quality Leg-In candle ATR multiplier (1.5x ATR)
    maxBaseAtrMult=1.0,       # Maximum base candle size (<= 1.0x ATR)
    maxWickPct=0.25,          # Maximum wick/shadow percentage in Leg-Out (25%)

    # --- Base & Leg-In Rules ---
    minBaseCount=1,           # Minimum base candles in zone (1)
    maxBaseCount=3,           # Maximum base candles in zone (3)
    legInMinAtrMult=1.0,      # Leg-In TR must be greater than base candle ATR (1.0x ATR)
    minClvPct=0.60,           # Minimum Close Location Value for Leg-In (60%)
    legInToBaseSizeMult=2.0,  # Leg-In size must be at least 2x larger than the largest base candle

    # --- Imbalance & Swing Settings ---
    useImbalance=True,        # Active imbalance check (Gap size must not exceed Leg-In TR)
    swingLeftBars=3,          # Swing left bars (3)
    swingRightBars=3,         # Swing right bars (3)
)


_HARD_MAX_BASE_COUNT = 3  # Hard limit locked to 3 base candles max


@dataclass
class Zone:
    proxVal: float                  # Proximal line (Entry price level)
    distVal: float                  # Distal line (Zone outer edge)
    slVal: float                    # Stop Loss level
    tpVal: float                    # Take Profit target
    isDemand: bool                  # True = Demand Zone, False = Supply Zone
    isHQ: bool                      # High Quality flag (True/False)
    densityScore: int               # Zone quality score (0 to 100)
    patternType: str = ""           # Pattern type: RBR, DBR, DBD, RBD
    zoneCategory: str = ""          # Category: Continuation or Reversal
    state: str = "Fresh"            # State: Fresh, Tested, or Broken
    touchCount: int = 0             # Number of re-tests
    originalDensityScore: int = 0   # Initial density score at creation
    startBarIndex: int = 0          # Zone start candle index
    createdBarIndex: int = 0        # Zone creation (Leg-Out) candle index
    baseCount: int = 0              # Number of base candles
    timestamp: object = None        # Creation timestamp
    qty: float = 0.0                # Trade quantity based on risk management
    sweptLiquidity: bool = False    # Liquidity sweep flag


def _wilder_atr(high, low, close, period):
    """Calculates Average True Range (ATR) using Wilder's Smoothing Method."""
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
    """Determines start bar index based on lookback months."""
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


def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
               lookback_months: Optional[float] = None) -> List[Zone]:
    """Core function to scan and detect Demand & Supply Zones."""
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
            preLegInIdx = legInIdx + 1

            if t - preLegInIdx < 0 or t - baseCount < 0:
                continue
            if np.isnan(atr[t - legInIdx]) or np.isnan(atr[t]):
                continue

            # --- LEG-IN CHECK ---
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

            # --- PRE-LEG-IN 50% COVERAGE RULE ---
            preLegInHigh = h[t - preLegInIdx]
            preLegInLow = l[t - preLegInIdx]
            preLegInIsBull = is_bull(t, preLegInIdx)
            preLegInIsBear = is_bear(t, preLegInIdx)

            isOppositeColorPreLegIn = (legInIsBull and preLegInIsBear) or (legInIsBear and preLegInIsBull)

            if isOppositeColorPreLegIn:
                overlapHigh = min(legInHigh, preLegInHigh)
                overlapLow = max(legInLow, preLegInLow)
                overlapRange = max(0.0, overlapHigh - overlapLow)

                if legInTR > 0 and (overlapRange / legInTR) >= 0.50:
                    continue

            bullClv = (legInClose - legInLow) / legInRng
            bearClv = (legInHigh - legInClose) / legInRng

            # --- BASE CHECK ---
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

            # --- LEG-OUT CHECK ---
            legOutTR = tr(t, legOutIdx)
            legOutHigh = h[t - legOutIdx]
            legOutLow = l[t - legOutIdx]
            legOutClose = c[t - legOutIdx]
            legOutVol = v[t - legOutIdx]

            isDemandLegOut = is_bull(t, legOutIdx)
            isSupplyLegOut = is_bear(t, legOutIdx)
            if not (isDemandLegOut or isSupplyLegOut):
                continue

            isLegOutExplosive = legOutTR >= (legOutMult * atr[t - legOutIdx])
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["maxWickPct"]
            passesTRHierarchy = (legOutTR > legInTR) and (legInTR > maxBaseTR)
            passesVolume = legOutVol > legInVol

            # --- PRICE IMBALANCE CHECK ---
            hasImbalance = True
            imbalanceGap = 0.0
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasImbalance = (legOutLow > maxBaseHigh) or (legOutClose > legInHigh)
                    if legOutLow > maxBaseHigh:
                        imbalanceGap = legOutLow - maxBaseHigh
                elif isSupplyLegOut:
                    hasImbalance = (legOutHigh < minBaseLow) or (legOutClose < legInLow)
                    if minBaseLow > legOutHigh:
                        imbalanceGap = minBaseLow - legOutHigh

                if hasImbalance and (imbalanceGap > legInTR):
                    hasImbalance = False

            # --- PATTERN CLASSIFICATION ---
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

            # --- DENSITY SCORE CALCULATION ---
            densityScore = 0

            if baseCount == 1:
                densityScore += 15

            if legInTR >= (p["hqLegInAtrMult"] * atr[t - legInIdx]):
                densityScore += 10

            if legOutTR >= (p["hqLegOutAtrMult"] * atr[t - legOutIdx]):
                densityScore += 15

            if (legInTR >= 2.0 * maxBaseTR) and (legOutTR >= 2.0 * legInTR):
                densityScore += 15

            if legOutVol > vol_sma[t - legOutIdx]:
                densityScore += 10

            if isDemandLegOut:
                legOutBodyPos = (legOutClose - legOutLow) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.50:
                    densityScore += 15
            else:
                legOutBodyPos = (legOutHigh - legOutClose) / legOutTR if legOutTR > 0 else 0
                if legOutBodyPos >= 0.50:
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

            densityScore += 10  # Freshness bonus

            isHQZone = densityScore >= 70

            # --- PROXIMAL, DISTAL, SL & TP ---
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            riskAmount = p["accountCapital"] * (p["riskPct"] / 100.0)
            qty = round(riskAmount / riskPerShare, 2) if riskPerShare > 0 else 0.0

            # --- DUPLICATE ZONE FILTER ---
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

        # --- ZONE STATE TRACKING ---
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
    """Returns active (Fresh or Tested) zones."""
    states = {"Fresh"} | ({"Tested"} if include_tested else set())
    return [z for z in zones if z.state in states]


def get_zone_alerts(zones, current_price, min_proximity_pct=0.0, max_proximity_pct=1.0,
                     include_tested=True) -> List[Dict[str, Any]]:
    """Generates live alerts for active zones near current price."""
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
