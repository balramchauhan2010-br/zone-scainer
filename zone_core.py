# -*- coding: utf-8 -*-

from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
import numpy as np
import pandas as pd
import datetime as _dt


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
    genuineGapScoreBonus=10,

    rejectOppositeCoverPct=0.50,

    minValidScore=40,
    hqScoreThreshold=90,
    legOutBodyHeavyPressurePct=0.60,

    testedLegOutRetracePct=0.50,
    maxTestedCount=2,

    # -------- SMART FIX PARAMS (NEW) ----------
    dropZeroVolumeBars=True,     # off-session fillers हटेंगे
    minValidVolume=1.0,          # volume >= 1 को valid मानें
    dropInvalidOhlc=True,        # NaN/inf OHLC drop
    filterSessionTimes=False,    # NSE session filter ON करना हो तो True करें
    sessionStart="09:15",        # exchange local time assumed
    sessionEnd="15:30",
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


def _parse_hhmm(s: str) -> _dt.time:
    hh, mm = s.strip().split(":")
    return _dt.time(int(hh), int(mm))


def _preprocess_df(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """
    SMART FIX:
    - drop NaN/inf OHLC
    - drop volume==0 fillers
    - optional session time filter (NSE intraday)
    """
    out = df.copy()
    out = out.sort_index()

    if p.get("dropInvalidOhlc", True):
        out = out.replace([np.inf, -np.inf], np.nan)
        out = out.dropna(subset=["open", "high", "low", "close", "volume"])

    if p.get("dropZeroVolumeBars", True):
        out = out[out["volume"].astype(float) >= float(p.get("minValidVolume", 1.0))]

    if p.get("filterSessionTimes", False) and isinstance(out.index, pd.DatetimeIndex):
        st = _parse_hhmm(p.get("sessionStart", "09:15"))
        en = _parse_hhmm(p.get("sessionEnd", "15:30"))
        # assumes index is already in exchange local time
        t = out.index.time
        mask = np.array([(ti >= st) and (ti <= en) for ti in t], dtype=bool)
        out = out.loc[mask]

    return out


def debug_alignment(df: pd.DataFrame, legout_timestamp, params: Optional[dict] = None,
                    base_counts=(1, 2, 3)) -> List[Dict[str, Any]]:
    """
    यह function आपको दिखाएगा कि scanner किस candles को leg-in/base मान रहा है।
    If alignment wrong => यही root-cause है।
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    dfx = _preprocess_df(df, p)
    if legout_timestamp not in dfx.index:
        raise KeyError("legout_timestamp not found AFTER preprocessing. (Maybe session filter/volume filter removed it.)")

    t = int(dfx.index.get_loc(legout_timestamp))
    res = []
    for bc in base_counts:
        legInIdx = bc + 1
        prevIdx = legInIdx + 1
        if t - prevIdx < 0:
            res.append({"baseCount": bc, "ok": False, "reason": "not enough bars"})
            continue
        base_ts = [dfx.index[t - b] for b in range(1, bc + 1)]
        res.append({
            "baseCount": bc,
            "ok": True,
            "legOut_ts": dfx.index[t],
            "base_ts": base_ts,
            "legIn_ts": dfx.index[t - legInIdx],
            "prev_ts": dfx.index[t - prevIdx],
        })
    return res


# --------------------------------------------------------------------------
# Core scan (your v8.5 logic, unchanged except preprocessing)
# --------------------------------------------------------------------------
def scan_zones(df: pd.DataFrame, params: Optional[dict] = None,
               lookback_months: Optional[float] = None) -> List[Zone]:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)

    p["maxBaseCount"] = min(int(p["maxBaseCount"]), _HARD_MAX_BASE_COUNT)
    p["minBaseCount"] = max(1, min(int(p["minBaseCount"]), p["maxBaseCount"]))

    # SMART FIX: preprocess first (this is the main change)
    df = _preprocess_df(df, p)

    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    n = len(df)

    if n < 50:
        return []

    atr = _wilder_atr(h, l, c, p["atrPeriod"])
    vol_sma = pd.Series(v).rolling(window=p["volSmaPeriod"], min_periods=1).mean().to_numpy()

    def tr(t, idx): return h[t - idx] - l[t - idx]
    def is_bull(t, idx): return c[t - idx] > o[t - idx]
    def is_bear(t, idx): return o[t - idx] > c[t - idx]

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
        return abs(c[i] - o[i]) / rng

    def body_high_low(t, idx):
        i = t - idx
        return max(o[i], c[i]), min(o[i], c[i])

    zones: List[Zone] = []
    active_zones: List[Zone] = []

    min_start = max(p["atrPeriod"], p["maxBaseCount"] + 3, 11)
    record_from_bar = max(min_start, _resolve_start_bar_for_lookback(df, lookback_months))

    legOutMult = p.get("legOutTrMult", p.get("legOutAtrMult", 1.2))

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

            if t - prevIdx < 0 or t - baseCount < 0:
                continue
            if np.isnan(atr[t - legInIdx]) or np.isnan(atr[t]):
                continue

            # --- LEG-IN ---
            legInTR = tr(t, legInIdx)
            legInLow = l[t - legInIdx]
            legInHigh = h[t - legInIdx]
            legInClose = c[t - legInIdx]
            legInVol = v[t - legInIdx]
            legInRng = legInHigh - legInLow

            legInIsBull = is_bull(t, legInIdx)
            legInIsBear = is_bear(t, legInIdx)
            if legInRng == 0 or not (legInIsBull or legInIsBear):
                continue

            # body strength (as your v8.5)
            if body_pct(t, legInIdx) < p["legInMinBodyPct"]:
                continue

            # opposite-color prev candle body cover
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

            # --- BASE ---
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
                maxBaseTR = max(maxBaseTR, bTR)
                maxBaseHigh = max(maxBaseHigh, h[t - b])
                minBaseLow = min(minBaseLow, l[t - b])

            if not allBaseValid or maxBaseTR == 0:
                continue

            if legInTR < (p["legInToBaseSizeMult"] * maxBaseTR):
                continue

            if legInTR < (p["legInMinAtrMult"] * atr[t - legInIdx]):
                continue

            # --- LEG-OUT ---
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

            hasImbalance = True
            hasGenuineGap = False
            gapSize = 0.0
            if p["useImbalance"]:
                if isDemandLegOut:
                    hasGenuineGap = legOutLow > maxBaseHigh
                    gapCond = hasGenuineGap or (legOutClose > legInHigh)
                    gapSize = max(0.0, legOutLow - maxBaseHigh)
                    hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
                else:
                    hasGenuineGap = legOutHigh < minBaseLow
                    gapCond = hasGenuineGap or (legOutClose < legInLow)
                    gapSize = max(0.0, minBaseLow - legOutHigh)
                    hasImbalance = gapCond and (gapSize <= p["maxImbalanceVsLegInMult"] * legInTR)
                if hasGenuineGap and gapSize > (p["maxImbalanceVsLegInMult"] * legInTR):
                    hasGenuineGap = False

            legOutBodyHigh = max(legOutOpen, legOutClose)
            legOutBodyLow = min(legOutOpen, legOutClose)
            legOutBodyEngulfsBase = (legOutBodyLow <= minBaseLow) and (legOutBodyHigh >= maxBaseHigh)
            if legOutBodyEngulfsBase and not hasGenuineGap:
                continue

            # --- PATTERN ---
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

            # --- SCORE ---
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
                if isSupplyLegOut and is_bull(t, b):
                    hasOppositeColorBase = True
                    break
            if hasOppositeColorBase:
                densityScore += 10

            densityScore += 10  # fresh bonus
            if hasGenuineGap:
                densityScore += p["genuineGapScoreBonus"]

            if densityScore < p["minValidScore"]:
                continue

            isHQZone = densityScore >= p["hqScoreThreshold"]
            zoneFoundOnThisBar = True

            # --- LEVELS ---
            proxVal = maxBaseHigh if isDemandLegOut else minBaseLow
            distVal = minBaseLow if isDemandLegOut else maxBaseHigh

            slVal = (distVal - p["slBufferAtr"] * atr[t]) if isDemandLegOut else (distVal + p["slBufferAtr"] * atr[t])
            riskPerShare = abs(proxVal - slVal)
            tpVal = (proxVal + riskPerShare * p["targetRR"]) if isDemandLegOut else (proxVal - riskPerShare * p["targetRR"])

            if isDemandLegOut:
                legOutMidLevel = legOutHigh - p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)
            else:
                legOutMidLevel = legOutLow + p["testedLegOutRetracePct"] * (legOutHigh - legOutLow)

            if isRBR:
                patternType, zoneCategory = "RBR", "Continuation"
            elif isDBR:
                patternType, zoneCategory = "DBR", "Reversal"
            elif isDBD:
                patternType, zoneCategory = "DBD", "Continuation"
            else:
                patternType, zoneCategory = "RBD", "Reversal"

            newZone = Zone(
                proxVal=proxVal, distVal=distVal, slVal=slVal, tpVal=tpVal,
                isDemand=isDemandLegOut, isHQ=isHQZone, densityScore=densityScore,
                patternType=patternType, zoneCategory=zoneCategory, state="Fresh",
                touchCount=0, originalDensityScore=densityScore,
                startBarIndex=t - baseCount, createdBarIndex=t, baseCount=baseCount,
                timestamp=df.index[t],
                legOutHigh=legOutHigh, legOutLow=legOutLow, legOutMidLevel=legOutMidLevel,
            )
            zones.append(newZone)
            active_zones.append(newZone)

        # --- STATE TRACKING (same) ---
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
