# -*- coding: utf-8 -*-
"""
zone_core.py  — v2 INSTITUTIONAL (ALL 4 PATTERNS, FULL VALIDATION)
==========================================================================
DBR (Demand/Reversal) | RBR (Demand/Continuation) |
RBD (Supply/Reversal) | DBD (Supply/Continuation)

हर zone: patternType ("DBR"/"RBR"/"RBD"/"DBD"), zoneCategory ("Reversal"/"Continuation")

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
    targetRR=5.0,
    slBufferAtr=0.1,
    atrPeriod=14,
    legOutAtrMult=1.2,
    hqLegOutAtr=2.0,
    maxBaseAtrMult=1.0,
    legOutMaxWickPct=0.25,
    legInMaxWickPct=0.35,
    legInMinClvPct=0.60,
    useSweepFilter=True,
    requireSweepRejectionClose=True,
    useImbalance=True,
    minBaseCount=1,
    maxBaseCount=3,
    reqLegInVol=True,
    legInMinMultOfBase=2.0,
    baseMaxBodyRatio=0.35,
    baseMinOverlapPct=0.50,
    baseVolMaxRatio=1.0,
    baseContractionBonus=True,
    legOutVolLookback=20,
    legOutVolMult=1.5,
    legacyProximalDistal=False,
    legInInclusionFactor=0.35,
    riskAtrMin=0.30,
    riskAtrMax=4.00,
    touchDecayPerTest=15,
    curveExtensionAtrMax=7.0,
    curveExtensionPenalty=20,
    enableRoleReversal=False,
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
    flipped: bool = False
    startBarIndex: int = 0
    createdBarIndex: int = 0
    baseCount: int = 0
    timestamp: object = None


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


def _pivots(high, low, left=5, right=5):
    n = len(high)
    swing_high = np.full(n, np.nan)
    swing_low = np.full(n, np.nan)
    window = left + right + 1
    if n < window:
        return swing_high, swing_low
    from numpy.lib.stride_tricks import sliding_window_view
    hw = sliding_window_view(high, window)
    lw = sliding_window_view(low, window)
    centers_h = high[left: n - right]
    centers_l = low[left: n - right]
    max_h = hw.max(axis=1)
    min_l = lw.min(axis=1)
    count_max = (hw == max_h[:, None]).sum(axis=1)
    count_min = (lw == min_l[:, None]).sum(axis=1)
    is_pivot_h = (centers_h == max_h) & (count_max == 1)
    is_pivot_l = (centers_l == min_l) & (count_min == 1)
    idx = np.arange(left, n - right)
    confirm_idx = idx + right
    swing_high[confirm_idx[is_pivot_h]] = centers_h[is_pivot_h]
    swing_low[confirm_idx[is_pivot_l]] = centers_l[is_pivot_l]
    return swing_high, swing_low


def _causal_last(values):
    return pd.Series(values).ffill().to_numpy()


def _clv_bearish(o, h, l, c):
    rng = h - l
    return 0.0 if rng <= 0 else (h - c) / rng


def _clv_bullish(o, h, l, c):
    rng = h - l
    return 0.0 if rng <= 0 else (c - l) / rng


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
    legInMinMultOfBase = p["legInMinMultOfBase"]
    legacy = p["legacyProximalDistal"]
    legInInclusionFactor = p["legInInclusionFactor"]

    atr = _wilder_atr(h, l, c, atrPeriod)
    swing_high_raw, swing_low_raw = _pivots(h, l, 5, 5)
    lastSwingHigh = _causal_last(swing_high_raw)
    lastSwingLow = _causal_last(swing_low_raw)

    def tr(t, idx): return h[t - idx] - l[t - idx]
    def is_bull(t, idx): return c[t - idx] > o[t - idx]
    def is_bear(t, idx): return o[t - idx] > c[t - idx]

    def wick_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]
        if rng == 0: return 0.0
        wicks = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])
        return wicks / rng

    zones: List[Zone] = []
    active_zones: List[Zone] = []
    min_start = max(atrPeriod, maxBaseCount + 2, 11)

    vol_series = pd.Series(v)
    lookback = p["legOutVolLookback"]
    rolling_avg_vol = vol_series.rolling(lookback, min_periods=1).mean().shift(1).to_numpy()

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

            legOutTR = tr(t, legOutIdx)
            legInTR = tr(t, legInIdx)
            legInLow = l[t - legInIdx]
            legInHigh = h[t - legInIdx]
            legInOpen = o[t - legInIdx]
            legInClose = c[t - legInIdx]

            allBaseValid = True
            maxBaseTR = 0.0
            maxBaseHigh = -1.0
            minBaseLow = 999999.0
            baseBodyHighMax = -1.0
            baseBodyLowMin = 999999.0
            base_trs = []
            base_ok = True

            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    base_ok = False
                    break
                bTR = tr(t, b)
                base_trs.append(bTR)
                if bTR > maxBaseTR: maxBaseTR = bTR
                if bTR > (p["maxBaseAtrMult"] * atr[t - b]): allBaseValid = False
                if h[t - b] > maxBaseHigh: maxBaseHigh = h[t - b]
                if l[t - b] < minBaseLow: minBaseLow = l[t - b]
                bodyHigh = max(o[t - b], c[t - b])
                bodyLow = min(o[t - b], c[t - b])
                if bodyHigh > baseBodyHighMax: baseBodyHighMax = bodyHigh
                if bodyLow < baseBodyLowMin: baseBodyLowMin = bodyLow
            if not base_ok:
                continue

            base_body_ratio_ok = True
            for b in range(1, baseCount + 1):
                bh, bl = h[t - b], l[t - b]
                rng = bh - bl
                if rng <= 0: continue
                body = abs(c[t - b] - o[t - b])
                if (body / rng) > p["baseMaxBodyRatio"]:
                    base_body_ratio_ok = False
                    break

            base_overlap_ok = True
            if baseCount >= 2:
                for b in range(1, baseCount):
                    h1, l1 = h[t - b], l[t - b]
                    h2, l2 = h[t - (b + 1)], l[t - (b + 1)]
                    overlap = min(h1, h2) - max(l1, l2)
                    min_rng = min(h1 - l1, h2 - l2)
                    if min_rng <= 0 or (overlap / min_rng) < p["baseMinOverlapPct"]:
                        base_overlap_ok = False
                        break

            base_vol_ok = True
            if baseCount >= 1:
                base_vols = [v[t - b] for b in range(1, baseCount + 1)]
                avg_base_vol = sum(base_vols) / len(base_vols)
                if avg_base_vol > (p["baseVolMaxRatio"] * v[t - legInIdx]):
                    base_vol_ok = False

            base_is_contracting = False
            if p["baseContractionBonus"] and len(base_trs) >= 2:
                base_is_contracting = all(
                    base_trs[i] >= base_trs[i + 1] * 0.9 for i in range(len(base_trs) - 1)
                )

            validLegIn = True
            if p["reqLegInVol"]:
                validLegIn = (
                    v[t - legInIdx] >= v[t - legInIdx - 1] * 0.8
                    and legInTR >= 0.8 * atr[t - legInIdx]
                )
            if maxBaseTR <= 0 or legInTR < (legInMinMultOfBase * maxBaseTR):
                validLegIn = False
            if wick_pct(t, legInIdx) > p["legInMaxWickPct"]:
                validLegIn = False

            legInIsBull = is_bull(t, legInIdx)
            legInIsBear = is_bear(t, legInIdx)

            legIn_clv_ok = True
            if legInIsBear:
                clv = _clv_bearish(legInOpen, legInHigh, legInLow, legInClose)
                legIn_clv_ok = clv >= p["legInMinClvPct"]
            elif legInIsBull:
                clv = _clv_bullish(legInOpen, legInHigh, legInLow, legInClose)
                legIn_clv_ok = clv >= p["legInMinClvPct"]
            else:
                legIn_clv_ok = False
            if not legIn_clv_ok:
                validLegIn = False

            passesVolume = v[t - legOutIdx] > v[t - legInIdx]
            avg_vol_lookback = rolling_avg_vol[t - legOutIdx]
            passesVolClimax = True
            if not np.isnan(avg_vol_lookback) and avg_vol_lookback > 0:
                passesVolClimax = v[t - legOutIdx] >= (p["legOutVolMult"] * avg_vol_lookback)

            isLegOutExplosive = legOutTR >= (p["legOutAtrMult"] * atr[t - legOutIdx])
            isLegOutWickValid = wick_pct(t, legOutIdx) <= p["legOutMaxWickPct"]
            isDemandLegOut = is_bull(t, legOutIdx)
            isSupplyLegOut = is_bear(t, legOutIdx)
            passesTRHierarchy = (legOutTR > legInTR) and (legInTR > maxBaseTR)

            isRBR = legInIsBull and isDemandLegOut
            isDBR = legInIsBear and isDemandLegOut
            isDBD = legInIsBear and isSupplyLegOut
            isRBD = legInIsBull and isSupplyLegOut

            hasBOS = False
            if isDemandLegOut:
                hasBOS = c[t - legOutIdx] > max(h[t - legInIdx], maxBaseHigh)
            elif isSupplyLegOut:
                hasBOS = c[t - legOutIdx] < min(l[t - legInIdx], minBaseLow)

            hasImbalance = True
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasImbalance = (l[t - legOutIdx] > maxBaseHigh) or (c[t - legOutIdx] > h[t - legInIdx])
                elif isSupplyLegOut:
                    hasImbalance = (h[t - legOutIdx] < minBaseLow) or (c[t - legOutIdx] < l[t - legInIdx])

            sweptLiquidity = False
            sweepRejectionOk = True
            if isDemandLegOut and not np.isnan(lastSwingLow[t]):
                sweptLiquidity = (minBaseLow < lastSwingLow[t]) or (legInLow < lastSwingLow[t])
                if sweptLiquidity and p["requireSweepRejectionClose"]:
                    sweepRejectionOk = legInClose > lastSwingLow[t]
            elif isSupplyLegOut and not np.isnan(lastSwingHigh[t]):
                sweptLiquidity = (maxBaseHigh > lastSwingHigh[t]) or (legInHigh > lastSwingHigh[t])
                if sweptLiquidity and p["requireSweepRejectionClose"]:
                    sweepRejectionOk = legInClose < lastSwingHigh[t]

            passesSweepCheck = True
            if p["useSweepFilter"]:
                passesSweepCheck = sweptLiquidity and sweepRejectionOk

            isValid = (
                (isRBR or isDBR or isDBD or isRBD)
                and allBaseValid and base_body_ratio_ok and base_overlap_ok and base_vol_ok
                and validLegIn and isLegOutExplosive and isLegOutWickValid
                and passesTRHierarchy and hasBOS and passesVolume and passesVolClimax
                and hasImbalance and passesSweepCheck
            )

            if isValid:
                zoneFoundOnThisBar = True
                densityScore = 25
                if legOutTR >= p["hqLegOutAtr"] * atr[t - legOutIdx]: densityScore += 25
                if sweptLiquidity: densityScore += 25
                if baseCount <= 2 and t - 1 >= 0 and not np.isnan(atr[t - 1]) and maxBaseTR <= 0.7 * atr[t - 1]:
                    densityScore += 25
                if base_is_contracting:
                    densityScore = min(100, densityScore + 10)
                isHQZone = densityScore >= 75

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
                risk_in_atr = riskPerShare / atr[t] if atr[t] > 0 else 0.0
                if risk_in_atr < p["riskAtrMin"] or risk_in_atr > p["riskAtrMax"]:
                    continue

                tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

                isDuplicate = False
                for i in range(0, min(len(zones), 11)):
                    checkZ = zones[len(zones) - 1 - i]
                    if checkZ.isDemand == isDemandLegOut and abs(checkZ.proxVal - proxVal) < (atr[t] * 0.25):
                        isDuplicate = True
                        break

                if not isDuplicate:
                    if isRBR: patternType, zoneCategory = "RBR", "Continuation"
                    elif isDBR: patternType, zoneCategory = "DBR", "Reversal"
                    elif isDBD: patternType, zoneCategory = "DBD", "Continuation"
                    else: patternType, zoneCategory = "RBD", "Reversal"

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

        if active_zones:
            lo_t, hi_t = l[t], h[t]
            still_active = []
            for z in active_zones:
                if z.state == "Fresh" and not np.isnan(atr[t]) and atr[t] > 0:
                    ref_price = c[t]
                    dist_atr = abs(ref_price - z.proxVal) / atr[t]
                    if dist_atr > p["curveExtensionAtrMax"]:
                        z.densityScore = max(10, z.densityScore - p["curveExtensionPenalty"])
                        z.isHQ = z.densityScore >= 75

                if z.state == "Fresh":
                    if z.isDemand:
                        if lo_t <= z.proxVal and lo_t > z.distVal:
                            z.state = "Tested"; z.touchCount += 1
                        elif lo_t <= z.distVal:
                            z.state = "Broken"
                    else:
                        if hi_t >= z.proxVal and hi_t < z.distVal:
                            z.state = "Tested"; z.touchCount += 1
                        elif hi_t >= z.distVal:
                            z.state = "Broken"
                elif z.state == "Tested":
                    still_touching = (
                        (z.isDemand and lo_t <= z.proxVal) or
                        ((not z.isDemand) and hi_t >= z.proxVal)
                    )
                    if (z.isDemand and lo_t <= z.distVal) or ((not z.isDemand) and hi_t >= z.distVal):
                        z.state = "Broken"
                    elif still_touching:
                        z.touchCount += 1
                        z.densityScore = max(10, z.densityScore - p["touchDecayPerTest"])
                        z.isHQ = z.densityScore >= 75

                if z.state != "Broken":
                    still_active.append(z)
                elif p["enableRoleReversal"] and not z.flipped:
                    z.flipped = True
                    flip = Zone(
                        proxVal=z.distVal, distVal=z.proxVal,
                        slVal=z.proxVal + (z.proxVal - z.distVal) * 0.1 * (1 if z.isDemand else -1),
                        tpVal=z.distVal, isDemand=not z.isDemand, isHQ=False, densityScore=30,
                        patternType="FLIP", zoneCategory="Flip", state="Fresh",
                        startBarIndex=z.startBarIndex, createdBarIndex=t, baseCount=z.baseCount,
                        timestamp=df.index[t],
                    )
                    zones.append(flip)
                    still_active.append(flip)
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
