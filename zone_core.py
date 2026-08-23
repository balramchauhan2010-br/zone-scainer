# -*- coding: utf-8 -*-

from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
import numpy as np
import pandas as pd


DEFAULT_PARAMS = dict(
    # --- risk/targets (qty removed as per your v8.5) ---
    accountCapital=25000.0,
    riskPct=0.5,
    targetRR=5.0,
    slBufferAtr=0.1,

    # --- indicators ---
    atrPeriod=14,
    volSmaPeriod=20,

    # --- leg-out rules ---
    legOutTrMult=1.2,
    legOutMinTrRatio=1.0,
    hqLegOutTrMult=2.0,
    maxWickPct=0.25,

    # NEW: gap होने पर wick rule relax (ICICI जैसे cases के लिए)
    relaxWickOnGenuineGap=True,
    maxWickPctOnGenuineGap=0.60,   # gap के साथ 60% तक wick allow (आप चाहें तो 0.45 कर दें)

    # --- base rules ---
    minBaseCount=1,
    maxBaseCount=3,
    maxBaseAtrMult=1.0,

    # --- leg-in rules ---
    legInMinAtrMult=1.0,
    minClvPct=0.60,                 # BUY/SELL pressure via CLV >= 60% (MANDATORY)
    legInToBaseSizeMult=2.0,         # legInTR >= 2 * maxBaseTR

    # IMPORTANT FIX:
    # पहले legInMinBodyPct mandatory था; अब optional कर दिया ताकि CLV-valid legIn reject न हो
    enforceLegInMinBodyPct=False,
    legInMinBodyPct=0.60,

    # opposite candle body-cover filter (same)
    rejectOppositeCoverPct=0.50,

    # imbalance rules (same)
    useImbalance=True,
    maxImbalanceVsLegInMult=1.0,
    genuineGapScoreBonus=10,

    # scoring thresholds (same)
    minValidScore=40,
    hqScoreThreshold=90,
    hqLegInAtrMult=1.5,
    legOutBodyHeavyPressurePct=0.60,

    # tested/broken rules (same)
    testedLegOutRetracePct=0.50,
    maxTestedCount=2,

    # duplicate filter (same, but tunable)
    duplicateAtrFrac=0.25,
    duplicateLookbackZones=11,

    # diagnostics
    diagnostics=False,              # True करने पर scan_zones में lastRejectReason attach करेगा
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


def _diagnose_candidate_at_index(df: pd.DataFrame, t: int, baseCount: int, params: dict) -> Dict[str, Any]:
    """
    Returns detailed booleans/values showing exactly which rule fails
    for a given legOut index t and chosen baseCount.
    """
    p = params
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    atr = _wilder_atr(h, l, c, p["atrPeriod"])

    def tr(idx): return h[idx] - l[idx]
    def is_bull(idx): return c[idx] > o[idx]
    def is_bear(idx): return o[idx] > c[idx]
    def wick_pct(idx):
        rng = h[idx] - l[idx]
        if rng <= 0: return 0.0
        w = (h[idx] - max(o[idx], c[idx])) + (min(o[idx], c[idx]) - l[idx])
        return w / rng
    def body_pct(idx):
        rng = h[idx] - l[idx]
        if rng <= 0: return 0.0
        return abs(c[idx] - o[idx]) / rng
    def body_high_low(idx): return max(o[idx], c[idx]), min(o[idx], c[idx])

    legOutIdx = t
    legInIdx = t - (baseCount + 1)
    prevIdx  = t - (baseCount + 2)
    baseIdxs = [t - b for b in range(1, baseCount + 1)]

    out: Dict[str, Any] = dict(baseCount=baseCount, t=t, timestamp=df.index[t])
    if legInIdx < 0 or prevIdx < 0 or min(baseIdxs) < 0:
        out["error"] = "Not enough bars"
        return out

    # Base
    maxBaseTR = 0.0
    maxBaseHigh = -np.inf
    minBaseLow = np.inf
    base_valid = True
    for bi in baseIdxs:
        if np.isnan(atr[bi]):
            base_valid = False
            break
        btr = tr(bi)
        maxBaseTR = max(maxBaseTR, btr)
        maxBaseHigh = max(maxBaseHigh, h[bi])
        minBaseLow = min(minBaseLow, l[bi])
        if btr > p["maxBaseAtrMult"] * atr[bi]:
            base_valid = False
    out.update(dict(base_valid=base_valid, maxBaseTR=maxBaseTR, maxBaseHigh=maxBaseHigh, minBaseLow=minBaseLow))

    # Leg-in
    legInTR = tr(legInIdx)
    legInRng = h[legInIdx] - l[legInIdx]
    bullClv = (c[legInIdx] - l[legInIdx]) / legInRng if legInRng > 0 else 0.0
    bearClv = (h[legInIdx] - c[legInIdx]) / legInRng if legInRng > 0 else 0.0
    legInIsBull, legInIsBear = is_bull(legInIdx), is_bear(legInIdx)
    legInCLVOK = (bullClv >= p["minClvPct"]) if legInIsBull else ((bearClv >= p["minClvPct"]) if legInIsBear else False)
    legInATRok = (not np.isnan(atr[legInIdx])) and (legInTR >= p["legInMinAtrMult"] * atr[legInIdx])
    legInDoubleBase = (maxBaseTR > 0) and (legInTR >= p["legInToBaseSizeMult"] * maxBaseTR)
    legInBodyOk = body_pct(legInIdx) >= p["legInMinBodyPct"]
    out.update(dict(
        legInTR=legInTR, legInATR=float(atr[legInIdx]), legInIsBull=legInIsBull, legInIsBear=legInIsBear,
        bullClv=bullClv, bearClv=bearClv, legInCLVOK=legInCLVOK,
        legInATRok=legInATRok, legInDoubleBase=legInDoubleBase, legInBodyOk=legInBodyOk,
        prevBodyHigh=body_high_low(prevIdx)[0], prevBodyLow=body_high_low(prevIdx)[1]
    ))

    # Opposite cover
    prevIsBull, prevIsBear = is_bull(prevIdx), is_bear(prevIdx)
    isOpposite = (legInIsBull and prevIsBear) or (legInIsBear and prevIsBull)
    if isOpposite and legInRng > 0:
        prevBH, prevBL = body_high_low(prevIdx)
        overlap = max(0.0, min(prevBH, h[legInIdx]) - max(prevBL, l[legInIdx]))
        coverPct = overlap / legInRng
    else:
        coverPct = 0.0
    out["oppCoverPct"] = coverPct
    out["oppCoverOK"] = (not isOpposite) or (coverPct < p["rejectOppositeCoverPct"])

    # Leg-out
    legOutTR = tr(legOutIdx)
    legOutIsBull, legOutIsBear = is_bull(legOutIdx), is_bear(legOutIdx)
    out.update(dict(legOutTR=legOutTR, legOutATR=float(atr[legOutIdx]), legOutIsBull=legOutIsBull, legOutIsBear=legOutIsBear))
    out["legOutExplosive"] = (not np.isnan(atr[legOutIdx])) and (legOutTR >= p["legOutTrMult"] * atr[legOutIdx])
    out["legOutWickPct"] = wick_pct(legOutIdx)

    # Imbalance/gap
    hasGenuineGap = False
    hasImbalance = True
    gapSize = 0.0
    if p["useImbalance"]:
        if legOutIsBull:  # demand
            hasGenuineGap = l[legOutIdx] > maxBaseHigh
            gapSize = max(0.0, l[legOutIdx] - maxBaseHigh)
            gapCond = hasGenuineGap or (c[legOutIdx] > h[legInIdx])
            hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
        elif legOutIsBear:  # supply
            hasGenuineGap = h[legOutIdx] < minBaseLow
            gapSize = max(0.0, minBaseLow - h[legOutIdx])
            gapCond = hasGenuineGap or (c[legOutIdx] < l[legInIdx])
            hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
    out.update(dict(hasImbalance=hasImbalance, hasGenuineGap=hasGenuineGap, gapSize=gapSize))

    # Wick valid with relaxation
    wickOk = out["legOutWickPct"] <= p["maxWickPct"]
    if (not wickOk) and p.get("relaxWickOnGenuineGap", False) and hasGenuineGap:
        wickOk = out["legOutWickPct"] <= p.get("maxWickPctOnGenuineGap", 0.60)
    out["legOutWickOK"] = wickOk

    # Hierarchy/volume
    out["trHierarchyOK"] = (legOutTR >= p["legOutMinTrRatio"] * legInTR) and (legInTR > maxBaseTR)
    out["volumeOK"] = v[legOutIdx] > v[legInIdx]

    # Engulf base body (blocked only if no genuine gap)
    legOutBodyHigh = max(o[legOutIdx], c[legOutIdx])
    legOutBodyLow = min(o[legOutIdx], c[legOutIdx])
    engulfsBase = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)
    out["legOutBodyEngulfsBase"] = engulfsBase
    out["engulfOK"] = (not engulfsBase) or hasGenuineGap

    # Pattern (CLV already enforced in leg-in check)
    out["patternRBR"] = legInIsBull and legOutIsBull
    out["patternDBR"] = legInIsBear and legOutIsBull
    out["patternDBD"] = legInIsBear and legOutIsBear
    out["patternRBD"] = legInIsBull and legOutIsBear

    # Final validity snapshot (as scanner will do)
    legInBodyGate = True
    if p.get("enforceLegInMinBodyPct", False):
        legInBodyGate = legInBodyOk

    out["finalValid"] = (
        base_valid
        and legInCLVOK
        and legInATRok
        and legInDoubleBase
        and out["oppCoverOK"]
        and (legOutIsBull or legOutIsBear)
        and out["legOutExplosive"]
        and out["legOutWickOK"]
        and out["trHierarchyOK"]
        and out["volumeOK"]
        and out["engulfOK"]
        and out["hasImbalance"]
        and (out["patternRBR"] or out["patternDBR"] or out["patternDBD"] or out["patternRBD"])
        and legInBodyGate
    )
    return out


def diagnose_zone_at_timestamp(df: pd.DataFrame, legout_timestamp, base_counts=(1, 2, 3),
                               params: Optional[dict] = None) -> List[Dict[str, Any]]:
    """
    Use this to know EXACTLY why ICICI 24-Jun-2026 09:15 (example) didn't scan.
    Returns diagnostics for each baseCount you try.
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    if legout_timestamp not in df.index:
        raise KeyError("legout_timestamp not found in df.index")

    t = int(df.index.get_loc(legout_timestamp))
    out = []
    for bc in base_counts:
        out.append(_diagnose_candidate_at_index(df, t, int(bc), p))
    return out


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

    atr = _wilder_atr(h, l, c, p["atrPeriod"])
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()

    def tr(t, idx): return h[t - idx] - l[t - idx]
    def is_bull(t, idx): return c[t - idx] > o[t - idx]
    def is_bear(t, idx): return o[t - idx] > c[t - idx]

    def wick_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]
        if rng <= 0:
            return 0.0
        wicks = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])
        return wicks / rng

    def body_pct(t, idx):
        i = t - idx
        rng = h[i] - l[i]
        if rng <= 0:
            return 0.0
        return abs(c[i] - o[i]) / rng

    def body_high_low(t, idx):
        i = t - idx
        return max(o[i], c[i]), min(o[i], c[i])

    zones: List[Zone] = []
    active_zones: List[Zone] = []

    min_start = max(p["atrPeriod"], p["maxBaseCount"] + 3, 11)
    record_from_bar = max(min_start, _resolve_start_bar_for_lookback(df, lookback_months))

    for t in range(min_start, n):
        if np.isnan(atr[t]):
            continue

        zoneFoundOnThisBar = False

        for baseCount in range(p["minBaseCount"], p["maxBaseCount"] + 1):
            if zoneFoundOnThisBar:
                break

            legOutIdx = 0
            legInIdx = baseCount + 1
            prevIdx = legInIdx + 1

            if t - prevIdx < 0:
                continue
            if np.isnan(atr[t - legInIdx]) or np.isnan(atr[t]):
                continue

            # ---------------- LEG-IN ----------------
            legInTR = tr(t, legInIdx)
            legInLow = l[t - legInIdx]
            legInHigh = h[t - legInIdx]
            legInClose = c[t - legInIdx]
            legInVol = v[t - legInIdx]
            legInRng = legInHigh - legInLow
            if legInRng <= 0:
                continue

            legInIsBull = is_bull(t, legInIdx)
            legInIsBear = is_bear(t, legInIdx)
            if not (legInIsBull or legInIsBear):
                continue

            bullClv = (legInClose - legInLow) / legInRng
            bearClv = (legInHigh - legInClose) / legInRng

            # FIX: Leg-In pressure via CLV is mandatory (as per your rule)
            if legInIsBull and bullClv < p["minClvPct"]:
                continue
            if legInIsBear and bearClv < p["minClvPct"]:
                continue

            # Optional: body strength gate (OFF by default to avoid false negatives)
            if p.get("enforceLegInMinBodyPct", False):
                if body_pct(t, legInIdx) < p["legInMinBodyPct"]:
                    continue

            # Opposite-color previous candle body cover rule (same)
            prevIsBull = is_bull(t, prevIdx)
            prevIsBear = is_bear(t, prevIdx)
            isOppositeColor = (legInIsBull and prevIsBear) or (legInIsBear and prevIsBull)
            if isOppositeColor:
                prevBodyHigh, prevBodyLow = body_high_low(t, prevIdx)
                overlap = max(0.0, min(prevBodyHigh, legInHigh) - max(prevBodyLow, legInLow))
                coverPct = overlap / legInRng
                if coverPct >= p["rejectOppositeCoverPct"]:
                    continue

            # ---------------- BASE ----------------
            allBaseValid = True
            maxBaseTR = 0.0
            maxBaseHigh = -1.0
            minBaseLow = float("inf")

            for b in range(1, baseCount + 1):
                if np.isnan(atr[t - b]):
                    allBaseValid = False
                    break
                bTR = tr(t, b)

                if bTR > (p["maxBaseAtrMult"] * atr[t - b]):
                    allBaseValid = False
                    break

                maxBaseTR = max(maxBaseTR, bTR)
                maxBaseHigh = max(maxBaseHigh, h[t - b])
                minBaseLow = min(minBaseLow, l[t - b])

            if not allBaseValid or maxBaseTR <= 0:
                continue

            # leg-in must be >= 2x biggest base TR
            if legInTR < (p["legInToBaseSizeMult"] * maxBaseTR):
                continue

            # leg-in TR >= ATR
            if legInTR < (p["legInMinAtrMult"] * atr[t - legInIdx]):
                continue

            # ---------------- LEG-OUT ----------------
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

            # Imbalance + genuine gap (compute BEFORE wick/engulf so we can relax wick on gap)
            hasImbalance = True
            hasGenuineGap = False
            gapSize = 0.0
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasGenuineGap = legOutLow > maxBaseHigh
                    gapSize = max(0.0, legOutLow - maxBaseHigh)
                    gapCond = hasGenuineGap or (legOutClose > legInHigh)
                    hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
                else:
                    hasGenuineGap = legOutHigh < minBaseLow
                    gapSize = max(0.0, minBaseLow - legOutHigh)
                    gapCond = hasGenuineGap or (legOutClose < legInLow)
                    hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
                if hasGenuineGap and gapSize > (p["maxImbalanceVsLegInMult"] * legInTR):
                    hasGenuineGap = False

            # Explosive + Wick (wick can relax if genuine gap)
            isLegOutExplosive = legOutTR >= (p["legOutTrMult"] * atr[t - legOutIdx])

            legOutW = wick_pct(t, legOutIdx)
            isLegOutWickValid = legOutW <= p["maxWickPct"]
            if (not isLegOutWickValid) and p.get("relaxWickOnGenuineGap", False) and hasGenuineGap:
                isLegOutWickValid = legOutW <= p.get("maxWickPctOnGenuineGap", 0.60)

            passesTRHierarchy = (legOutTR >= p["legOutMinTrRatio"] * legInTR) and (legInTR > maxBaseTR)
            passesVolume = legOutVol > legInVol

            # Engulf base body rule (same logic as your v8.6 fix)
            legOutBodyHigh = max(legOutOpen, legOutClose)
            legOutBodyLow = min(legOutOpen, legOutClose)
            legOutBodyEngulfsBase = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)
            if legOutBodyEngulfsBase and not hasGenuineGap:
                continue

            # ---------------- PATTERN ----------------
            isRBR = legInIsBull and isDemandLegOut
            isDBR = legInIsBear and isDemandLegOut
            isDBD = legInIsBear and isSupplyLegOut
            isRBD = legInIsBull and isSupplyLegOut

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

            # ---------------- SCORE ----------------
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

            # Close position bonus
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

            # Opposite-color base bonus
            hasOppositeColorBase = False
            for b in range(1, baseCount + 1):
                if isDemandLegOut and is_bear(t, b):
                    hasOppositeColorBase = True
                    break
                if isSupplyLegOut and is_bull(t, b):
                    hasOppositeColorBase = True
                    break
            if hasOppositeColorBase:
                densityScore += 10

            # Fresh bonus
            densityScore += 10

            # Genuine gap bonus
            if hasGenuineGap:
                densityScore += p["genuineGapScoreBonus"]

            if densityScore < p["minValidScore"]:
                continue

            isHQZone = densityScore >= p["hqScoreThreshold"]
            zoneFoundOnThisBar = True

            # ---------------- LEVELS ----------------
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            # Tested mid level (leg-out based)
            if isDemandLegOut:
                legOutMidLevel = legOutHigh - p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)
            else:
                legOutMidLevel = legOutLow + p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)

            # Duplicate filter
            isDuplicate = False
            checked = 0
            for checkZ in reversed(zones):
                if checkZ.state == "Broken":
                    continue
                if checkZ.isDemand == isDemandLegOut and abs(checkZ.proxVal - proxVal) < (atr[t] * p["duplicateAtrFrac"]):
                    isDuplicate = True
                    break
                checked += 1
                if checked >= p["duplicateLookbackZones"]:
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

        # --- STATE TRACKING ---
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
            "distance_pct": diff_pct * 100, "state": z.state, "timestamp": z.timestamp,
        })
    alerts.sort(key=lambda a: (-int(a["is_hq"]), a["distance_pct"]))
    return alerts
