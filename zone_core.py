from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# Structural check only: no density/HQ scoring.
BASE_BORING_MAX_BODY_PCT = 0.55
PINE_DEFAULTS: Dict[str, Any] = {
    "accountCapital": 25000.0, "riskPct": 0.5, "targetRR": 5.0,
    "slBufferAtr": 0.1, "atrPeriod": 14, "volSmaPeriod": 20,
    "legOutTrMult": 1.2, "legOutMinTrRatio": 1.0,
    "maxBaseAtrMult": 1.0, "maxWickPct": 0.30,
    "minBaseCountInput": 1, "maxBaseCountInput": 3,
    "legInMinAtrMult": 1.0, "minClvPct": 0.60,
    "legInToBaseSizeMult": 2.0, "legOutToLegInBodyMult": 1.0,
    "legInMinBodyPct": 0.55, "useImbalance": True,
    # Legacy compatibility keys: these two gap-cap settings are not applied
    # by the supplied strategy, and are deliberately not activated here.
    "maxImbalanceMult": 1.0, "relaxGapCapOvernight": True,
    "rejectOppositeCoverPct": 0.50, "testedLegOutRetracePct": 1.00,
    "maxTestedCount": 1,
}
# Compatibility with existing imports. settings() returns a fresh dictionary.
DEFAULT_PARAMS = dict(PINE_DEFAULTS)


@dataclass
class Box:
    left: int
    top: float
    right: int
    bottom: float
    border_color: object
    bgcolor: object

    def set_right(self, right: int) -> None:
        self.right = right

    def set_bgcolor(self, color: object) -> None:
        self.bgcolor = color

    def set_border_color(self, color: object) -> None:
        self.border_color = color


@dataclass
class Zone:
    proxVal: float
    distVal: float
    slVal: float
    tpVal: float
    isDemand: bool
    densityScore: int
    isHQ: bool
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
    zoneBox: Box
    timestamp: object = None
    riskPct: float = float("nan")  # Entry-to-SL distance %, not account risk %.
    score10: float = 0.0
    baseColourOK: bool = False
    legInVolX: float = float("nan")
    legOutVolX: float = float("nan")
    retestVolX: float = float("nan")
    entryStatus: str = ""
    entryPrice: float = 0.0
    gapToLegIn: float = 0.0


def settings(accountCapital: Optional[float] = None, **overrides: Any) -> Dict[str, Any]:
    config = dict(PINE_DEFAULTS)
    if accountCapital is not None:
        overrides["accountCapital"] = accountCapital
    config.update({k: v for k, v in overrides.items() if k in config})
    integer_keys = {"atrPeriod", "volSmaPeriod", "minBaseCountInput",
                    "maxBaseCountInput", "maxTestedCount"}
    boolean_keys = {"useImbalance", "relaxGapCapOvernight"}
    for key, value in config.items():
        if key in boolean_keys:
            if not isinstance(value, (bool, np.bool_)):
                raise ValueError(f"{key} must be a boolean")
            config[key] = bool(value)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be numeric") from exc
        if not np.isfinite(number):
            raise ValueError(f"{key} must be finite")
        if key in integer_keys:
            minimum = 0 if key == "maxTestedCount" else 1
            if not number.is_integer() or number < minimum:
                raise ValueError(f"{key} must be an integer >= {minimum}")
            config[key] = int(number)
        else:
            if number < 0:
                raise ValueError(f"{key} must be non-negative")
            config[key] = number
    for key in ("accountCapital", "targetRR", "legOutTrMult", "maxBaseAtrMult"):
        if config[key] <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("maxWickPct", "minClvPct", "legInMinBodyPct",
                "rejectOppositeCoverPct", "testedLegOutRetracePct"):
        if not 0 <= config[key] <= 1:
            raise ValueError(f"{key} must be between 0 and 1")
    if config["riskPct"] > 100:
        raise ValueError("riskPct must be between 0 and 100")
    if not 1 <= config["minBaseCountInput"] <= config["maxBaseCountInput"] <= 3:
        raise ValueError("Base counts must satisfy 1 <= min <= max <= 3")
    if config["legOutToLegInBodyMult"] < 1:
        raise ValueError("legOutToLegInBodyMult must be >= 1")
    return config


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Validate a single-symbol OHLCV frame; do not silently fix bad candles."""
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("OHLCV data requires a DatetimeIndex")
    if isinstance(df.columns, pd.MultiIndex):
        raise ValueError("Extract one ticker before passing MultiIndex data")
    frame = df.copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    required = ["open", "high", "low", "close"]
    if frame.columns.duplicated().any() or not set(required).issubset(frame.columns):
        raise ValueError("Unique open, high, low, close columns are required")
    if frame.index.hasnans or frame.index.has_duplicates:
        raise ValueError("Timestamps must be valid and unique")
    frame = frame.sort_index()
    if "volume" not in frame:
        frame["volume"] = 0.0
    frame = frame[required + ["volume"]].apply(pd.to_numeric, errors="raise")
    frame["volume"] = frame["volume"].fillna(0.0)
    if not np.isfinite(frame.to_numpy(dtype=float)).all():
        raise ValueError("OHLCV contains missing or non-finite values")
    if (frame["volume"] < 0).any():
        raise ValueError("Volume cannot be negative")
    if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Invalid OHLC: high/low must enclose open and close")
    return frame.astype(float)


class ZoneEngine:
    def __init__(self, df: pd.DataFrame, **kwargs: Any):
        self.df = normalize_ohlcv(df)
        for key, value in settings(**kwargs).items():
            setattr(self, key, value)
        self.minBaseCount = self.minBaseCountInput
        self.maxBaseCount = self.maxBaseCountInput
        self.open, self.high, self.low, self.close, self.volume = (
            self.df[c].to_numpy(dtype=float)
            for c in ("open", "high", "low", "close", "volume")
        )
        self.n = len(self.df)
        self.active_zones: List[Zone] = []  # Includes historical Broken zones.
        self.live_zones: List[Zone] = []
        self._prepare_indicators()

    @staticmethod
    def _rma(series: np.ndarray, length: int) -> np.ndarray:
        result = np.full(len(series), np.nan)
        if len(series) >= length:
            result[length - 1] = np.mean(series[:length])
            for i in range(length, len(series)):
                result[i] = result[i - 1] + (series[i] - result[i - 1]) / length
        return result

    def _prepare_indicators(self) -> None:
        if self.n:
            previous = np.r_[self.close[0], self.close[:-1]]
            self.current_tr = np.maximum(
                self.high - self.low,
                np.maximum(abs(self.high - previous), abs(self.low - previous)),
            )
            self.current_tr[0] = self.high[0] - self.low[0]
        else:
            self.current_tr = np.empty(0)
        self.atr_val = self._rma(self.current_tr, self.atrPeriod)
        self.vol_sma = self.df.volume.rolling(self.volSmaPeriod).mean().to_numpy()

    def _body_pct(self, pos: int) -> float:
        size = self.high[pos] - self.low[pos]
        return abs(self.close[pos] - self.open[pos]) / size if size else 0.0

    def _vol_x(self, pos: int) -> float:
        avg = self.vol_sma[pos]
        return float(self.volume[pos] / avg) if np.isfinite(avg) and avg > 0 else float("nan")

    def _is_overnight_gap(self, i: int) -> bool:
        # Compare full local dates, not weekday numbers or timestamp units.
        return i > 0 and self.df.index[i].date() != self.df.index[i - 1].date()

    def _scan_bar(self, i: int) -> None:
        for count in range(self.minBaseCount, self.maxBaseCount + 1):
            lin, prev = i - count - 1, i - count - 2
            if prev < 0 or np.isnan(self.atr_val[lin]):
                continue
            lin_tr = self.current_tr[lin]
            lin_range = self.high[lin] - self.low[lin]
            lin_bull = self.close[lin] > self.open[lin]
            lin_bear = self.close[lin] < self.open[lin]
            if lin_range == 0 or self._body_pct(lin) < self.legInMinBodyPct:
                continue
            opposite = ((lin_bull and self.close[prev] < self.open[prev])
                        or (lin_bear and self.close[prev] > self.open[prev]))
            if opposite:
                prev_hi = max(self.open[prev], self.close[prev])
                prev_lo = min(self.open[prev], self.close[prev])
                overlap = max(0.0, min(prev_hi, self.high[lin]) - max(prev_lo, self.low[lin]))
                if overlap / lin_range >= self.rejectOppositeCoverPct:
                    continue

            base = slice(i - count, i)
            base_tr = self.current_tr[base]
            base_body = abs(self.close[base] - self.open[base])
            base_atr = self.atr_val[base]
            if np.isnan(base_atr).any() or (base_tr > self.maxBaseAtrMult * base_atr).any():
                continue
            max_tr, max_body = float(max(base_tr)), float(max(base_body))
            body_ratios = np.divide(base_body, base_tr, out=np.zeros_like(base_body), where=base_tr > 0)
            if max_tr == 0 or (body_ratios > BASE_BORING_MAX_BODY_PCT).any():
                continue
            base_hi, base_lo = float(max(self.high[base])), float(min(self.low[base]))
            size_mult = 1.5 if count == 1 else self.legInToBaseSizeMult
            if lin_tr < size_mult * max_tr or lin_tr < self.legInMinAtrMult * self.atr_val[lin]:
                continue

            out_tr = self.current_tr[i]
            out_hi, out_lo = self.high[i], self.low[i]
            out_open, out_close = self.open[i], self.close[i]
            demand, supply = out_close > out_open, out_close < out_open
            if not (demand or supply):
                continue
            # Original full-engulf rejection is preserved.
            if out_hi >= base_hi and out_lo <= base_lo:
                continue
            lin_body, out_body = abs(self.close[lin] - self.open[lin]), abs(out_close - out_open)
            wick_pct = ((out_hi - max(out_open, out_close)) + (min(out_open, out_close) - out_lo)) / (out_hi - out_lo) if out_hi != out_lo else 0.0
            if not (
                out_tr > self.legOutTrMult * self.atr_val[i]
                and wick_pct <= self.maxWickPct
                and out_tr >= self.legOutMinTrRatio * lin_tr
                and max_tr < lin_tr < out_tr
                and max_body < lin_body
                and out_body > self.legOutToLegInBodyMult * lin_body
                and (out_close > base_hi if demand else out_close < base_lo)
                and (self.volume[i] <= 0 or self.volume[i] > self.volume[lin])
            ):
                continue

            genuine_gap, imbalance, gap_size = False, True, 0.0
            if self.useImbalance:
                genuine_gap = out_lo > base_hi if demand else out_hi < base_lo
                imbalance = genuine_gap or (out_close > self.high[lin] if demand else out_close < self.low[lin])
                gap_size = max(0.0, out_lo - base_hi if demand else base_lo - out_hi)
            if min(out_open, out_close) <= base_lo and max(out_open, out_close) >= base_hi and not genuine_gap:
                continue
            bull_clv = (self.close[lin] - self.low[lin]) / lin_range
            bear_clv = (self.high[lin] - self.close[lin]) / lin_range
            valid_lin = (lin_bull and bull_clv >= self.minClvPct) or (lin_bear and bear_clv >= self.minClvPct)
            if not imbalance or not valid_lin:
                continue
            pattern = ("RBR" if lin_bull else "DBR") if demand else ("RBD" if lin_bull else "DBD")
            prox, dist = (base_hi, base_lo) if demand else (base_lo, base_hi)
            sl = dist - self.slBufferAtr * self.atr_val[i] if demand else dist + self.slBufferAtr * self.atr_val[i]
            risk = abs(prox - sl)
            tp = prox + risk * self.targetRR if demand else prox - risk * self.targetRR
            # Original one-zone-per-bar + last 11 live zones duplicate check.
            if any(z.isDemand == demand and abs(z.proxVal - prox) < self.atr_val[i] * 0.25
                   for z in self.live_zones[-11:]):
                break
            color = "green" if demand else "red"
            zone = Zone(
                proxVal=prox, distVal=dist, slVal=sl, tpVal=tp,
                isDemand=bool(demand), densityScore=0, isHQ=False,
                patternType=pattern,
                zoneCategory="Continuation" if pattern in ("RBR", "DBD") else "Reversal",
                state="Fresh", touchCount=0, startBarIndex=i-count,
                createdBarIndex=i, baseCount=count, legOutHigh=out_hi,
                legOutLow=out_lo,
                legOutMidLevel=(out_hi - self.testedLegOutRetracePct * (out_hi-out_lo)
                                if demand else out_lo + self.testedLegOutRetracePct * (out_hi-out_lo)),
                isOvernight=self._is_overnight_gap(i), legInTR=lin_tr, legOutTR=out_tr,
                zoneBox=Box(i-count-1, max(prox, dist), i+15, min(prox, dist), color, (color, 0.15)),
                timestamp=self.df.index[i], riskPct=risk / prox * 100 if prox else float("nan"),
                legInVolX=self._vol_x(lin), legOutVolX=self._vol_x(i), gapToLegIn=gap_size,
            )
            self.active_zones.append(zone)
            self.live_zones.append(zone)
            break

    def _update_zone_states(self, i: int) -> None:
        for zone in self.live_zones[:]:
            if i <= zone.createdBarIndex:
                zone.zoneBox.set_right(i + 15)
                continue
            broken = self.low[i] <= zone.distVal if zone.isDemand else self.high[i] >= zone.distVal
            touched = self.low[i] <= zone.proxVal if zone.isDemand else self.high[i] >= zone.proxVal
            if broken:
                zone.state = "Broken"
            elif touched:
                zone.state = "Tested"
                zone.touchCount += 1
                if zone.touchCount > self.maxTestedCount:
                    zone.state = "Broken"
            if zone.state == "Broken":
                zone.zoneBox.set_bgcolor(("gray", 0.05))
                zone.zoneBox.set_border_color(("gray", 0.20))
                self.live_zones.remove(zone)
            else:
                zone.zoneBox.set_right(i + 15)

    def run(self) -> List[Zone]:
        # Reset so repeated run() calls do not duplicate/mutate old results.
        self.active_zones, self.live_zones = [], []
        for i in range(max(self.atrPeriod, self.maxBaseCount + 3, 11), self.n):
            # Preserve supplied policy: last row updates states, never creates a zone.
            if i < self.n - 1 and not np.isnan(self.atr_val[i]):
                self._scan_bar(i)
            self._update_zone_states(i)
        return self.active_zones


def scan_zones(df: pd.DataFrame, params: Optional[Dict[str, Any]] = None,
               accountCapital: Optional[float] = None) -> List[Zone]:
    incoming = dict(params or {})
    if accountCapital is not None:
        incoming["accountCapital"] = accountCapital
    return ZoneEngine(df, **settings(**incoming)).run()


def latest_active_zones(zones: List[Zone]) -> List[Zone]:
    return [z for z in zones if z.state in ("Fresh", "Tested")]


def get_zone_alerts(zones: List[Zone], price: float) -> List[Zone]:
    return [z for z in latest_active_zones(zones)
            if min(z.proxVal, z.distVal) <= price <= max(z.proxVal, z.distVal)]


def recommended_trade_setup(accountCapital: Optional[float] = None) -> Dict[str, Any]:
    config = settings(accountCapital=accountCapital)
    return {"patterns": ["RBR", "DBR", "DBD", "RBD"], "targetRR": config["targetRR"],
            "risk_pct": config["riskPct"], "capital": config["accountCapital"],
            "slBufferAtr": config["slBufferAtr"], "entry_mode": "prox"}


def backtest_summary(zones: List[Zone], df: pd.DataFrame) -> Dict[str, Any]:
    # Zone-state counts only; this is not a trade backtest.
    return {"n_zones": len(zones), "n_active": len(latest_active_zones(zones)),
            "n_broken": sum(z.state == "Broken" for z in zones)}


def realistic_roi(zones: List[Zone], df: pd.DataFrame, rr: float = 5.0,
                  risk_pct: float = 0.5, capital: float = 25000.0,
                  patterns: Optional[List[str]] = None, buffer: float = 0.1,
                  entry_mode: str = "prox", max_hold: int = 40) -> Dict[str, Any]:
    # Do not report dummy zero returns as measured performance.
    return {"implemented": False, "n_trades": None, "win_pct": None,
            "net_roi_pct": None,
            "sample_zones": sum(not patterns or z.patternType in patterns for z in zones),
            "risk_pct": risk_pct, "capital": capital, "targetRR": rr,
            "message": "Trade backtest is not implemented."}


def target_context(zone: Zone, df: Optional[pd.DataFrame] = None,
                   htf_df: Optional[pd.DataFrame] = None,
                   market_df: Optional[pd.DataFrame] = None,
                   vix: Optional[float] = None,
                   spx_ret20: Optional[float] = None) -> Dict[str, Any]:
    return {"score": None, "max": 6, "label": "—", "why": [],
            **dict.fromkeys("ABCDEF")}


def resample_nse_minutes(df: pd.DataFrame, minutes: int,
                         session_start: str = "09:15",
                         session_end: str = "15:30") -> pd.DataFrame:
    """Left-labelled IST bars; final short session bar is retained.

    Naive timestamps are interpreted as IST. Aware timestamps are converted.
    Resampling cannot reconstruct missing source candles.
    """
    if isinstance(minutes, bool) or int(minutes) != minutes or minutes <= 0:
        raise ValueError("minutes must be a positive integer")
    frame = normalize_ohlcv(df)
    frame.index = (frame.index.tz_localize("Asia/Kolkata") if frame.index.tz is None
                   else frame.index.tz_convert("Asia/Kolkata"))
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    pieces = []
    for date, day in frame.groupby(frame.index.date):
        day = day.between_time(session_start, session_end, inclusive="left")
        if day.empty:
            continue
        origin = pd.Timestamp(f"{date} {session_start}", tz="Asia/Kolkata")
        result = day.resample(f"{int(minutes)}min", origin=origin,
                              label="left", closed="left").agg(agg)
        pieces.append(result.dropna(subset=["open", "high", "low", "close"]))
    return pd.concat(pieces).sort_index() if pieces else frame.iloc[:0]


def resample_nse_session(df: pd.DataFrame, n_hours: int,
                         session_start: str = "09:15",
                         session_end: str = "15:30") -> pd.DataFrame:
    if isinstance(n_hours, bool) or int(n_hours) != n_hours or n_hours <= 0:
        raise ValueError("n_hours must be a positive integer")
    return resample_nse_minutes(df, int(n_hours) * 60, session_start, session_end)
