from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from universe import Instrument, TIMEFRAMES
from zone_core import normalize_ohlcv, resample_nse_minutes


OHLC = ["open", "high", "low", "close"]


def extract_ticker(data: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame:
    """Handle Yahoo's single- and multi-ticker MultiIndex column layouts."""
    if data.empty:
        raise ValueError("Yahoo returned no rows")
    frame = data
    if isinstance(data.columns, pd.MultiIndex):
        for level in range(data.columns.nlevels):
            if ticker in data.columns.get_level_values(level):
                frame = data.xs(ticker, axis=1, level=level).copy()
                break
        else:
            raise ValueError(f"Ticker absent in response: {ticker}")
    elif not single:
        raise ValueError("Unexpected flat columns in a multi-ticker response")
    frame = frame.copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    if not set(OHLC).issubset(frame.columns):
        raise ValueError("Missing OHLC columns")
    # Outer-joined ticker downloads contain all-NaN rows from other exchanges.
    frame = frame.dropna(subset=OHLC)
    frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
    if frame.empty:
        raise ValueError("No valid OHLC rows; ticker may be unavailable or rate-limited")
    return normalize_ohlcv(frame)


def download_batch(tickers: tuple[str, ...], interval: str, days: int
                   ) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Bounded batches; no overlapping yf.download calls across timeframes."""
    frames, errors = {}, {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    for offset in range(0, len(tickers), 30):
        batch = tickers[offset:offset + 30]
        try:
            data = yf.download(
                list(batch), start=start, end=end, interval=interval,
                group_by="ticker", auto_adjust=False, actions=False,
                ignore_tz=False, progress=False, threads=4, timeout=20,
            )
        except Exception as exc:
            for ticker in batch:
                errors[ticker] = f"Download failed: {exc}"
            continue
        for ticker in batch:
            try:
                frames[ticker] = extract_ticker(data, ticker, len(batch) == 1)
            except (ValueError, KeyError, TypeError) as exc:
                errors[ticker] = str(exc)
    return frames, errors


def prepare_timeframe(raw: pd.DataFrame, tf: str, item: Instrument) -> pd.DataFrame:
    frame = normalize_ohlcv(raw)
    interval, minutes, _ = TIMEFRAMES[tf]
    if item.nse:
        frame.index = (frame.index.tz_localize("Asia/Kolkata") if frame.index.tz is None
                       else frame.index.tz_convert("Asia/Kolkata"))
        if minutes:
            return resample_nse_minutes(frame, minutes)
        return frame
    if frame.index.tz is None:
        # Intraday exchange timezone must not be silently guessed.
        if minutes:
            raise ValueError("Global intraday response has no timezone")
        return frame
    if minutes:
        frame.index = frame.index.tz_convert("UTC")
        native = {"5m": 5, "30m": 30, "60m": 60}[interval]
        if native != minutes:
            # Explicit UTC-clock aggregation, not an exchange-session calendar.
            frame = frame.resample(
                f"{minutes}min", origin="start_day", label="left", closed="left",
            ).agg({"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"}).dropna(subset=OHLC)
    return frame


def scan_fingerprint(frame: pd.DataFrame, params: dict[str, Any]) -> str:
    """Invalidate on OHLCV revisions, index changes, or parameter changes."""
    digest = hashlib.sha256(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    digest.update(str(frame.index.dtype).encode())
    digest.update("|".join(frame.columns).encode())
    digest.update(json.dumps(params, sort_keys=True, allow_nan=False).encode())
    return digest.hexdigest()
