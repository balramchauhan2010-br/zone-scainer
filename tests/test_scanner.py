from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from data_service import extract_ticker, prepare_timeframe, scan_fingerprint
from universe import Instrument, stock_instrument
from zone_core import (DEFAULT_PARAMS, ZoneEngine, normalize_ohlcv, realistic_roi,
                       resample_nse_minutes, scan_zones, settings)


def sample(tail=True):
    candles = [[100, 100.2, 99.8, 100.02, 100] for _ in range(20)]
    candles += [[100, 102.1, 99.9, 102, 100],
                [102, 102.4, 101.9, 102.1, 100],
                [102.2, 105.1, 102, 105, 200]]
    if tail:
        candles.append([105, 105.2, 104.8, 105.1, 100])
    return pd.DataFrame(candles, columns=["open", "high", "low", "close", "volume"],
                        index=pd.date_range("2025-01-01", periods=len(candles), freq="D", tz="Asia/Kolkata"))


def append_candle(frame, candle):
    extra = pd.DataFrame([candle], columns=frame.columns,
                         index=[frame.index[-1] + pd.Timedelta(days=1)])
    return pd.concat([frame, extra])


def test_default_api_and_input_not_mutated():
    frame = sample()
    original = frame.copy(deep=True)
    zones = scan_zones(frame, DEFAULT_PARAMS, accountCapital=100000)
    assert len(zones) == 1
    zone = zones[0]
    assert (zone.patternType, zone.state, zone.touchCount) == ("RBR", "Fresh", 0)
    assert zone.densityScore == 0 and not zone.isHQ
    assert zone.slVal < zone.distVal < zone.proxVal < zone.tpVal
    assert zone.tpVal - zone.proxVal == pytest.approx((zone.proxVal - zone.slVal) * 5)
    pd.testing.assert_frame_equal(frame, original)


def test_last_candle_never_creates_zone():
    assert scan_zones(sample(tail=False)) == []


def test_first_and_second_touch():
    frame = append_candle(sample(), [103, 103.2, 102.3, 103.1, 80])
    zone = scan_zones(frame)[0]
    assert (zone.state, zone.touchCount) == ("Tested", 1)
    # No departure required: two consecutive touching candles break it.
    frame = append_candle(frame, [103, 103.2, 102.2, 103.1, 80])
    zone = scan_zones(frame)[0]
    assert (zone.state, zone.touchCount) == ("Broken", 2)


def test_distal_touch_breaks_without_waiting_for_sl():
    frame = append_candle(sample(), [103, 103.2, 101.9, 103.1, 80])
    assert scan_zones(frame)[0].state == "Broken"


def test_supply_box_and_levels():
    frame = sample()
    frame[["open", "close"]] = 200 - frame[["open", "close"]]
    old_high = frame.high.copy()
    frame["high"], frame["low"] = 200 - frame.low, 200 - old_high
    zone = scan_zones(frame)[0]
    assert zone.patternType == "DBD" and not zone.isDemand
    assert zone.zoneBox.top > zone.zoneBox.bottom
    assert zone.tpVal < zone.proxVal < zone.distVal < zone.slVal


@pytest.mark.parametrize("params", [
    {"atrPeriod": 0}, {"volSmaPeriod": 1.5}, {"accountCapital": -1},
    {"riskPct": np.inf}, {"minBaseCountInput": 3, "maxBaseCountInput": 1},
    {"maxBaseCountInput": 4}, {"maxWickPct": 2}, {"useImbalance": "false"},
])
def test_bad_settings(params):
    with pytest.raises(ValueError):
        settings(**params)


def test_engine_rerun_is_idempotent():
    engine = ZoneEngine(sample())
    first, second = engine.run(), engine.run()
    assert len(first) == len(second) == 1
    assert first[0] is not second[0]


def test_empty_and_missing_volume():
    frame = sample()
    assert scan_zones(frame.iloc[:0]) == []
    assert scan_zones(frame.drop(columns="volume"))[0].state == "Fresh"


def test_bad_ohlc_rejected():
    frame = sample()
    frame.loc[frame.index[0], "high"] = 90
    with pytest.raises(ValueError, match="Invalid OHLC"):
        normalize_ohlcv(frame)


def test_nse_anchor_even_if_first_bar_missing():
    frame = pd.DataFrame({"open": [100.] * 4, "high": [101.] * 4,
                          "low": [99.] * 4, "close": [100.] * 4},
                         index=pd.date_range("2025-01-02 09:20", periods=4, freq="5min"))
    result = resample_nse_minutes(frame, 15)
    assert [x.strftime("%H:%M") for x in result.index] == ["09:15", "09:30"]
    assert str(result.index.tz) == "Asia/Kolkata"


def test_nse_no_overnight_bars():
    frame = sample().iloc[:2].copy()
    frame.index = pd.DatetimeIndex(["2025-01-02 15:15", "2025-01-03 09:15"], tz="Asia/Kolkata")
    result = resample_nse_minutes(frame, 240)
    assert [x.strftime("%d %H:%M") for x in result.index] == ["02 13:15", "03 09:15"]


@pytest.mark.parametrize("reverse", [False, True])
def test_yahoo_single_ticker_multiindex(reverse):
    raw = pd.concat({"TCS.NS": sample()}, axis=1)
    if reverse:
        raw = raw.swaplevel(axis=1)
    result = extract_ticker(raw, "TCS.NS", True)
    pd.testing.assert_frame_equal(result, sample().astype(float))


def test_cache_detects_intrabar_changes_and_params():
    frame = sample()
    initial = scan_fingerprint(frame, settings())
    frame.loc[frame.index[-1], "low"] = 102.3
    assert initial != scan_fingerprint(frame, settings())
    assert initial != scan_fingerprint(sample(), settings(targetRR=3))


def test_global_not_filtered_to_nse_session():
    frame = sample().iloc[:4].copy()
    frame.index = pd.date_range("2025-01-02 00:00", periods=4, freq="h", tz="UTC")
    result = prepare_timeframe(frame, "2 Hours", Instrument("Test", "TEST", "TEST:TEST"))
    assert len(result) == 2
    assert result.index[0].hour == 0


def test_roi_not_misrepresented_as_measured_zero():
    result = realistic_roi([], sample())
    assert result["implemented"] is False
    assert result["net_roi_pct"] is None


def test_stock_symbol_mapping():
    assert stock_instrument("BAJAJ_AUTO").ticker == "BAJAJ-AUTO.NS"
    assert stock_instrument("tcs.ns").ticker == "TCS.NS"


def test_streamlit_initial_page():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    app.run(timeout=30)
    assert not app.exception
    assert any("Demand & Supply" in title.value for title in app.title)


def test_streamlit_scan_with_offline_data():
    # No network access: all selected symbols get synthetic OHLCV.
    frame = sample()
    for _ in range(3):
        frame = append_candle(frame, [105, 105.2, 104.8, 105.1, 100])

    def fake_download(tickers, interval, days):
        return {ticker: frame.copy() for ticker in tickers}, {}

    with patch("data_service.download_batch", side_effect=fake_download):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        app.run(timeout=30)
        # Only NSE + Daily, reducing the offline test work.
        app.sidebar.multiselect[0].set_value(["NSE watchlist"])
        app.run(timeout=30)
        tf_widget = next(widget for widget in app.sidebar.multiselect if widget.label == "Timeframes")
        tf_widget.set_value(["Daily"])
        next(button for button in app.sidebar.button if button.label == "🔍 Scan Zones").click()
        app.run(timeout=30)
        assert not app.exception
        assert app.metric[0].value != "0"
        assert app.metric[3].value == "0"


@pytest.mark.parametrize("mirror,pattern", [(False, "DBR"), (True, "RBD")])
def test_reversal_patterns(mirror, pattern):
    frame = sample()
    frame.iloc[20] = [104, 104.1, 101.9, 102, 100]
    frame.iloc[22] = [102.2, 107.1, 102, 107, 200]
    frame.iloc[23] = [107, 107.2, 106.8, 107.1, 100]
    if mirror:
        frame[["open", "close"]] = 220 - frame[["open", "close"]]
        old_high = frame.high.copy()
        frame["high"], frame["low"] = 220 - frame.low, 220 - old_high
    assert scan_zones(frame)[0].patternType == pattern


def test_leg_out_volume_rule():
    frame = sample()
    frame.iloc[22, frame.columns.get_loc("volume")] = 100
    assert scan_zones(frame) == []
    frame.iloc[22, frame.columns.get_loc("volume")] = 0
    assert len(scan_zones(frame)) == 1


def test_base_must_be_boring():
    frame = sample()
    frame.iloc[21] = [101.95, 102.4, 101.9, 102.35, 100]
    assert scan_zones(frame) == []
