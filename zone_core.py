"""
zone_core.py
========================================================================
Pine Script v6 "Zone" Indicator ka Python port.
Rules/Conditions/Thresholds bilkul same rakhe gaye hain.
Streamlit app (app.py) yahi file import karta hai:
    from zone_core import scan_zones, latest_active_zones, DEFAULT_PARAMS
========================================================================
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import pandas as pd
import numpy as np


# ==============================================================================
# 1. PARAMETERS & CONSTANTS
# ==============================================================================
@dataclass
class Params:
    accountCapital: float = 25000.0          # original Pine me bhi unused hai (preserved)
    riskPct: float = 0.5                     # unused in original logic (preserved)
    targetRR: float = 5.0
    slBufferAtr: float = 0.1

    atrPeriod: int = 14
    volSmaPeriod: int = 20
    legOutTrMult: float = 1.2
    legOutMinTrRatio: float = 1.0
    hqLegOutTrMult: float = 2.0
    hqLegInAtrMult: float = 1.5
    maxBaseAtrMult: float = 1.0
    maxWickPct: float = 0.30

    minBaseCountInput: int = 1
    maxBaseCountInput: int = 3
    legInMinAtrMult: float = 1.0
    minClvPct: float = 0.60
    legInToBaseSizeMult: float = 2.0
    legInMinBodyPct: float = 0.60

    useImbalance: bool = True
    maxImbalanceMult: float = 1.0            # unused in original logic (preserved)
    relaxGapCapOvernight: bool = True        # unused in original logic (preserved)
    genuineGapBonus: int = 10
    overnightGapBonus: int = 15
    rejectOppositeCoverPct: float = 0.50

    minValidScore: int = 40
    hqScoreThreshold: int = 90
    legOutBodyHeavyPct: float = 0.60

    testedLegOutRetracePct: float = 0.50
    maxTestedCount: int = 2

    HARD_MAX_BASE_COUNT: int = 3

    minBaseCount: int = field(init=False)
    maxBaseCount: int = field(init=False)

    def __post_init__(self):
        self.minBaseCount = max(1, min(self.minBaseCountInput, self.maxBaseCountInput))
        self.maxBaseCount = min(self.maxBaseCountInput, self.HARD_MAX_BASE_COUNT)


# app.py isi naam se import karta hai
DEFAULT_PARAMS = Params()


# ==============================================================================
# ZONE TYPE (Pine "type Zone" ka equivalent)
# ==============================================================================
@dataclass
class Zone:
    proxVal: float
    distVal: float
    slVal: float
    tpVal: float
    isDemand: bool
    isHQ: bool
    densityScore: int
    patternType: str
    zoneCategory: str
    state: str
    touchCount: int
    startBarIndex: int
    createdBarIndex: int
    baseCount: int
    legOutHigh: float
    legOutLow: float
    legOutMidLevel: float
    isOvernight: bool
    legInTR: float
    legOutTR: float
    box_left: int
    box_right: int
    box_top: float
    box_bottom: float
    box_border_color: str = ""
    box_bg_color: str = ""


# ==============================================================================
# 2. HELPERS: ta.rma / ta.sma (exact Pine semantics)
# ==============================================================================
def _compute_rma(series: np.ndarray, length: int) -> List[Optional[float]]:
    n = len(series)
    result: List[Optional[float]] = [None] * n
    for i in range(n):
        if i < length - 1:
            result[i] = None
        elif i == length - 1:
            result[i] = float(np.mean(series[0:length]))
        else:
            result[i] = (result[i - 1] * (length - 1) + series[i]) / length
    return result


def _compute_sma(series: np.ndarray, length: int) -> List[Optional[float]]:
    n = len(series)
    result: List[Optional[float]] = [None] * n
    for i in range(n):
        if i < length - 1:
            result[i] = None
        else:
            result[i] = float(np.mean(series[i - length + 1:i + 1]))
    return result


# ==============================================================================
# 3. MAIN SCANNING ENGINE  -> scan_zones()
# ==============================================================================
def scan_zones(df: pd.DataFrame, params: Params = DEFAULT_PARAMS) -> Tuple[List[Zone], List[float]]:
    """
    df: columns ['open','high','low','close','volume'] required.
        Index DatetimeIndex ho ya 'time' column ho (overnight-gap detection ke liye).
    Return: (all_zones_list, close_prices_list)
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        if 'time' in df.columns:
            times = pd.to_datetime(df['time'])
        else:
            raise ValueError("DataFrame me DatetimeIndex ya 'time' column hona chahiye.")
    else:
        times = df.index

    op = df['open'].to_numpy(dtype=float)
    hi = df['high'].to_numpy(dtype=float)
    lo = df['low'].to_numpy(dtype=float)
    cl = df['close'].to_numpy(dtype=float)
    vol = df['volume'].to_numpy(dtype=float)

    n = len(df)
    dow = pd.DatetimeIndex(times).dayofweek.to_numpy()
    time_vals = pd.DatetimeIndex(times).to_pydatetime()

    # ---------------- current_tr (gap-aware true range) ----------------
    current_tr = np.zeros(n)
    for i in range(n):
        tr = hi[i] - lo[i]
        if i > 0:
            tr = max(tr, abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
        current_tr[i] = tr

    atr_val = _compute_rma(current_tr, params.atrPeriod)
    vol_sma = _compute_sma(vol, params.volSmaPeriod)

    def is_overnight_gap(i: int) -> bool:
        if i == 0:
            return False
        if dow[i] != dow[i - 1]:
            return True
        delta_ms = (time_vals[i] - time_vals[i - 1]).total_seconds() * 1000.0
        return delta_ms > 86400000.0

    active_zones: List[Zone] = []
    closes_plot: List[float] = []

    min_start = max(params.atrPeriod, params.maxBaseCount + 3, 11)

    for i in range(n):
        closes_plot.append(cl[i])

        def pos_of(idx: int) -> int:
            return i - idx

        def TR(idx: int) -> float:
            p = pos_of(idx)
            if p < 0:
                return 0.0
            if p == 0:
                return hi[p] - lo[p]
            prev_close = cl[p - 1]
            return max(hi[p] - lo[p], abs(hi[p] - prev_close), abs(lo[p] - prev_close))

        def is_bull(idx: int) -> bool:
            p = pos_of(idx)
            return cl[p] > op[p]

        def is_bear(idx: int) -> bool:
            p = pos_of(idx)
            return op[p] > cl[p]

        def wick_pct(idx: int) -> float:
            p = pos_of(idx)
            rng = hi[p] - lo[p]
            if rng == 0:
                return 0.0
            wick = (hi[p] - max(op[p], cl[p])) + (min(op[p], cl[p]) - lo[p])
            return wick / rng

        def body_pct(idx: int) -> float:
            p = pos_of(idx)
            rng = hi[p] - lo[p]
            if rng == 0:
                return 0.0
            return abs(cl[p] - op[p]) / rng

        def body_high_low(idx: int):
            p = pos_of(idx)
            return max(op[p], cl[p]), min(op[p], cl[p])

        # ==================================================================
        # SCANNING ENGINE
        # ==================================================================
        if i >= min_start and atr_val[i] is not None:
            zoneFoundOnThisBar = False

            for bCount in range(params.minBaseCount, params.maxBaseCount + 1):
                if zoneFoundOnThisBar:
                    break

                legOutIdx = 0
                legInIdx = bCount + 1
                prevIdx = legInIdx + 1

                posLegIn = i - legInIdx
                if posLegIn < 0 or atr_val[posLegIn] is None:
                    continue

                legInTR = TR(legInIdx)
                posLI = pos_of(legInIdx)
                legInLow = lo[posLI]
                legInHigh = hi[posLI]
                legInClose = cl[posLI]
                legInVol = vol[posLI]
                legInRng = legInHigh - legInLow

                legInIsBull = is_bull(legInIdx)
                legInIsBear = is_bear(legInIdx)

                if legInRng == 0 or body_pct(legInIdx) < params.legInMinBodyPct:
                    continue

                posPrev = pos_of(prevIdx)
                if posPrev < 0:
                    continue

                prevIsBull = is_bull(prevIdx)
                prevIsBear = is_bear(prevIdx)
                isOppositeColor = (legInIsBull and prevIsBear) or (legInIsBear and prevIsBull)

                shouldRejectOverlap = False
                if isOppositeColor:
                    prevBodyHigh, prevBodyLow = body_high_low(prevIdx)
                    overlap = max(0.0, min(prevBodyHigh, legInHigh) - max(prevBodyLow, legInLow))
                    coverPct = overlap / legInRng
                    if coverPct >= params.rejectOppositeCoverPct:
                        shouldRejectOverlap = True

                if shouldRejectOverlap:
                    continue

                bullClv = (legInClose - legInLow) / legInRng
                bearClv = (legInHigh - legInClose) / legInRng

                allBaseValid = True
                maxBaseTR = 0.0
                maxBaseHigh = -1.0
                minBaseLow = 1_000_000_000.0

                for b in range(1, bCount + 1):
                    posB = pos_of(b)
                    if posB < 0 or atr_val[posB] is None:
                        allBaseValid = False
                        break
                    bTR = TR(b)
                    if bTR > (params.maxBaseAtrMult * atr_val[posB]):
                        allBaseValid = False
                        break
                    if bTR > maxBaseTR:
                        maxBaseTR = bTR
                    if hi[posB] > maxBaseHigh:
                        maxBaseHigh = hi[posB]
                    if lo[posB] < minBaseLow:
                        minBaseLow = lo[posB]

                if not allBaseValid or maxBaseTR == 0:
                    continue

                effectiveBaseSizeMult = 1.5 if bCount == 1 else params.legInToBaseSizeMult
                if legInTR < (effectiveBaseSizeMult * maxBaseTR):
                    continue

                validLegIn = legInTR >= (params.legInMinAtrMult * atr_val[posLI])
                if not validLegIn:
                    continue

                posLO = pos_of(legOutIdx)
                legOutTR = TR(legOutIdx)
                legOutHigh = hi[posLO]
                legOutLow = lo[posLO]
                legOutClose = cl[posLO]
                legOutOpen = op[posLO]
                legOutVol = vol[posLO]

                isDemandLegOut = is_bull(legOutIdx)
                isSupplyLegOut = is_bear(legOutIdx)

                if not (isDemandLegOut or isSupplyLegOut):
                    continue

                isLegOutExplosive = legOutTR >= (params.legOutTrMult * atr_val[posLO])
                isLegOutWickValid = wick_pct(legOutIdx) <= params.maxWickPct
                passesTRHierarchy = (legOutTR >= params.legOutMinTrRatio * legInTR) and (legInTR > maxBaseTR)
                passesVolume = legOutVol > legInVol

                isOvernight = is_overnight_gap(i)

                hasImbalance = True
                hasGenuineGap = False

                if params.useImbalance:
                    if isDemandLegOut:
                        hasGenuineGap = legOutLow > maxBaseHigh
                        gapCond = hasGenuineGap or (legOutClose > legInHigh)
                        hasImbalance = gapCond
                    elif isSupplyLegOut:
                        hasGenuineGap = legOutHigh < minBaseLow
                        gapCond = hasGenuineGap or (legOutClose < legInLow)
                        hasImbalance = gapCond

                legOutBodyHigh = max(legOutOpen, legOutClose)
                legOutBodyLow = min(legOutOpen, legOutClose)
                legOutBodyEngulfsBase = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)

                if legOutBodyEngulfsBase and not hasGenuineGap:
                    continue

                isRBR = legInIsBull and (bullClv >= params.minClvPct) and isDemandLegOut
                isDBR = legInIsBear and (bearClv >= params.minClvPct) and isDemandLegOut
                isDBD = legInIsBear and (bearClv >= params.minClvPct) and isSupplyLegOut
                isRBD = legInIsBull and (bullClv >= params.minClvPct) and isSupplyLegOut

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

                densityScore = 0
                if bCount == 1:
                    densityScore += 15
                if legInTR >= (params.hqLegInAtrMult * atr_val[posLI]):
                    densityScore += 10
                if legOutTR >= (params.hqLegOutTrMult * legInTR):
                    densityScore += 15
                if (legInTR >= 2.0 * maxBaseTR) and (legOutTR >= 2.0 * legInTR):
                    densityScore += 15
                if vol_sma[posLO] is not None and legOutVol > vol_sma[posLO]:
                    densityScore += 10

                if isDemandLegOut:
                    rng_lo = legOutHigh - legOutLow
                    legOutBodyPos = (legOutClose - legOutLow) / rng_lo if rng_lo > 0 else 0.0
                    legOutOwnBodyPct = body_pct(legOutIdx)
                    if isDBR:
                        if (legOutBodyPos >= 0.80) or (legOutOwnBodyPct >= params.legOutBodyHeavyPct):
                            densityScore += 15
                    else:
                        if legOutBodyPos >= 0.80:
                            densityScore += 15
                else:
                    rng_lo = legOutHigh - legOutLow
                    legOutBodyPos = (legOutHigh - legOutClose) / rng_lo if rng_lo > 0 else 0.0
                    if legOutBodyPos >= 0.80:
                        densityScore += 15

                hasOppositeColorBase = False
                for b in range(1, bCount + 1):
                    if isDemandLegOut and is_bear(b):
                        hasOppositeColorBase = True
                        break
                    elif isSupplyLegOut and is_bull(b):
                        hasOppositeColorBase = True
                        break

                if hasOppositeColorBase:
                    densityScore += 10

                densityScore += 10

                if hasGenuineGap:
                    densityScore += params.genuineGapBonus
                if isOvernight and hasGenuineGap:
                    densityScore += params.overnightGapBonus

                if densityScore < params.minValidScore:
                    continue

                isHQZone = densityScore >= params.hqScoreThreshold
                zoneFoundOnThisBar = True

                proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
                distVal = minBaseLow if isDemandLegOut else maxBaseHigh

                curAtr = atr_val[i]
                slVal = (distVal - params.slBufferAtr * curAtr) if isDemandLegOut else \
                        (distVal + params.slBufferAtr * curAtr)
                riskPerShare = abs(proxVal - slVal)
                tpVal = (proxVal + riskPerShare * params.targetRR) if isDemandLegOut else \
                        (proxVal - riskPerShare * params.targetRR)

                legOutMidLevel = (legOutHigh - params.testedLegOutRetracePct * (legOutHigh - legOutLow)) \
                    if isDemandLegOut else \
                    (legOutLow + params.testedLegOutRetracePct * (legOutHigh - legOutLow))

                isDuplicate = False
                checked = 0
                if len(active_zones) > 0:
                    for idxz in range(len(active_zones) - 1, -1, -1):
                        checkZ = active_zones[idxz]
                        if checkZ.state == "Broken":
                            continue
                        if checkZ.isDemand == isDemandLegOut and abs(checkZ.proxVal - proxVal) < (curAtr * 0.25):
                            isDuplicate = True
                            break
                        checked += 1
                        if checked >= 11:
                            break

                if isDuplicate:
                    continue

                patternType = "RBR" if isRBR else ("DBR" if isDBR else ("DBD" if isDBD else "RBD"))
                zoneCat = "Continuation" if (isRBR or isDBD) else "Reversal"

                boxBorderColor = "green" if isDemandLegOut else "red"
                boxFillColor = "rgba(0,255,0,0.15)" if isDemandLegOut else "rgba(255,0,0,0.15)"

                newZone = Zone(
                    proxVal=proxVal, distVal=distVal, slVal=slVal, tpVal=tpVal,
                    isDemand=isDemandLegOut, isHQ=isHQZone, densityScore=densityScore,
                    patternType=patternType, zoneCategory=zoneCat, state="Fresh",
                    touchCount=0, startBarIndex=i - bCount, createdBarIndex=i, baseCount=bCount,
                    legOutHigh=legOutHigh, legOutLow=legOutLow, legOutMidLevel=legOutMidLevel,
                    isOvernight=isOvernight, legInTR=legInTR, legOutTR=legOutTR,
                    box_left=i - bCount - 1, box_right=i + 15, box_top=proxVal, box_bottom=distVal,
                    box_border_color=boxBorderColor, box_bg_color=boxFillColor,
                )
                active_zones.append(newZone)

        # ---------------- STATE TRACKING (Fresh -> Tested -> Broken) ----------------
        if len(active_zones) > 0:
            lo_t = lo[i]
            hi_t = hi[i]

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

                if z.state == "Tested" and z.touchCount > params.maxTestedCount:
                    z.state = "Broken"

                if z.state == "Broken":
                    z.box_bg_color = "rgba(128,128,128,0.05)"
                    z.box_border_color = "rgba(128,128,128,0.2)"
                else:
                    z.box_right = i + 15

    return active_zones, closes_plot


# ==============================================================================
# 4. HELPER: app.py isko bhi import karta hai
# ==============================================================================
def latest_active_zones(zones: List[Zone], only_non_broken: bool = True) -> List[Zone]:
    """Sabse latest (naye) zones sabse upar, chahe to sirf non-broken zones filter karein."""
    filtered = [z for z in zones if (z.state != "Broken")] if only_non_broken else list(zones)
    filtered.sort(key=lambda z: z.createdBarIndex, reverse=True)
    return filtered


def zones_to_dataframe(zones: List[Zone]) -> pd.DataFrame:
    rows = []
    for z in zones:
        rows.append({
            "Pattern": z.patternType,
            "Category": z.zoneCategory,
            "Type": "Demand" if z.isDemand else "Supply",
            "HQ": z.isHQ,
            "Score": z.densityScore,
            "State": z.state,
            "Touches": z.touchCount,
            "Proximal": round(z.proxVal, 2),
            "Distal": round(z.distVal, 2),
            "SL": round(z.slVal, 2),
            "TP": round(z.tpVal, 2),
            "CreatedBar": z.createdBarIndex,
            "BaseCount": z.baseCount,
        })
    return pd.DataFrame(rows)
