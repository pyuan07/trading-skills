"""Indicator math. Each vectorized pandas implementation is cross-checked against an
INDEPENDENT scalar reference written here (plain Python loops, same definition) — so a
transposed subtraction, wrong span, or flipped clip in the production code breaks the test.
The reference encodes the *convention the code uses*: EWM with adjust=False seeded at the
first valid observation (Wilder alpha=1/n for RSI/ATR, span alpha=2/(span+1) for MACD)."""
import math

import pandas as pd
import pytest

from fetch_data import rsi, macd, bollinger, atr, obv, ret, compute_indicators, earnings_proximity


# deterministic price path: gentle uptrend + wobble, enough length for SMA/MACD to warm up
CLOSES = [100 + i * 0.5 + 3 * math.sin(i / 4) for i in range(120)]


def _ewm(values, alpha):
    """adjust=False EWM seeded at the first element — matches pandas on a leading-NaN-free series."""
    out = values[0]
    for v in values[1:]:
        out = out + alpha * (v - out)
    return out


def _ref_rsi(closes, n=14):
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    ag, al = _ewm(gains, 1 / n), _ewm(losses, 1 / n)
    rs = ag / al if al else float("inf")
    return 100 - 100 / (1 + rs)


def _ref_ema(values, span):
    return _ewm(values, 2 / (span + 1))


def test_rsi_matches_reference():
    assert rsi(pd.Series(CLOSES)) == pytest.approx(_ref_rsi(CLOSES), rel=1e-9)


def test_rsi_all_gains_is_100():
    rising = pd.Series([100 + i for i in range(30)])
    assert rsi(rising) == pytest.approx(100.0)


def test_macd_matches_reference():
    line, sig, hist, hist_prev = macd(pd.Series(CLOSES))
    # reference EMAs over the full series
    ema12 = [_ref_ema(CLOSES[: i + 1], 12) for i in range(len(CLOSES))]
    ema26 = [_ref_ema(CLOSES[: i + 1], 26) for i in range(len(CLOSES))]
    ref_line_series = [a - b for a, b in zip(ema12, ema26)]
    ref_sig_series = [_ref_ema(ref_line_series[: i + 1], 9) for i in range(len(ref_line_series))]
    assert line == pytest.approx(ref_line_series[-1], rel=1e-9)
    assert sig == pytest.approx(ref_sig_series[-1], rel=1e-9)
    assert hist == pytest.approx(ref_line_series[-1] - ref_sig_series[-1], rel=1e-9)
    # hist_prev is the histogram 5 bars back
    assert hist_prev == pytest.approx(ref_line_series[-6] - ref_sig_series[-6], rel=1e-9)


def test_bollinger_mid_is_sma20_and_bands_symmetric():
    s = pd.Series(CLOSES)
    mid, up, lo = bollinger(s)
    assert mid == pytest.approx(sum(CLOSES[-20:]) / 20, rel=1e-12)
    # upper/lower symmetric about mid
    assert (up - mid) == pytest.approx(mid - lo, rel=1e-9)
    # width = 2 * k * stddev(sample, ddof=1) — documents the ddof=1 convention
    sd = pd.Series(CLOSES[-20:]).std()  # ddof=1
    assert (up - mid) == pytest.approx(2.0 * sd, rel=1e-9)


def test_atr_matches_reference():
    n = len(CLOSES)
    high = [c + 1.0 for c in CLOSES]
    low = [c - 1.0 for c in CLOSES]
    df = pd.DataFrame({"High": high, "Low": low, "Close": CLOSES})
    # reference true range: first row has no prev close -> NaN, skipped by the EWM seed
    tr = []
    for i in range(n):
        hl = high[i] - low[i]
        if i == 0:
            tr.append(hl)  # pandas: first TR = H-L (hc/lc are NaN and max ignores them)
        else:
            hc = abs(high[i] - CLOSES[i - 1])
            lc = abs(low[i] - CLOSES[i - 1])
            tr.append(max(hl, hc, lc))
    assert atr(df) == pytest.approx(_ewm(tr, 1 / 14), rel=1e-9)


def test_obv_direction_and_accumulation():
    df = pd.DataFrame({"Close": [10, 11, 10, 10, 12], "Volume": [100, 200, 150, 300, 400]})
    o = obv(df).tolist()
    # +vol on up day, -vol on down day, 0 on flat day
    assert o == [0, 200, 50, 50, 450]


def test_ret_and_short_history_guard():
    s = pd.Series([100, 110])
    assert ret(s, 1) == pytest.approx(0.1)
    assert ret(s, 5) is None  # not enough history


def test_compute_indicators_pure_and_currency_passthrough():
    n = 260
    closes = [100 + i * 0.3 for i in range(n)]
    df = pd.DataFrame({
        "Open": closes, "High": [c + 1 for c in closes], "Low": [c - 1 for c in closes],
        "Close": closes, "Volume": [1_000_000] * n,
    }, index=pd.date_range("2025-01-01", periods=n, freq="B"))
    ind = compute_indicators(df, "sgd")
    assert ind["ccy"] == "SGD"                 # normalized upper
    assert ind["sma50"] == pytest.approx(sum(closes[-50:]) / 50, rel=1e-12)
    assert ind["sma200"] == pytest.approx(sum(closes[-200:]) / 200, rel=1e-12)
    assert ind["last"] == closes[-1]


# ---------- writer edge cases ----------

def _full_ind(**over):
    ind = {
        "last": 1250.0, "prev": 1240.0, "ccy": "USD",
        "sma50": 1201.33, "sma200": 1100.5,
        "macd_line": 5.0, "macd_sig": 3.0, "macd_hist": 2.0, "macd_hist_prev": 1.0,
        "bb_mid": 1230.0, "bb_up": 1310.0, "bb_lo": 1150.0,
        "rsi14": 55.0, "atr14": 25.0,
        "obv_trend": "rising", "obv_confirms": True,
        "vol_avg20": 1_000_000.0, "last_vol": 1_200_000.0,
        "ytd_ret": 0.12, "hi52": 1400.0, "lo52": 900.0,
    }
    ind.update(over)
    return ind


def test_write_technical_levels_survive_thousands_separators(tmp_path):
    # fmt() puts ',' inside numbers >= 1,000 — the KEY POINTS levels must not be
    # truncated at that comma ("support Bollinger mid 1" was the bug).
    from fetch_data import write_technical
    out = tmp_path / "technical.md"
    write_technical("BIGPX", _full_ind(), out)
    text = out.read_text(encoding="utf-8")
    levels = next(ln for ln in text.splitlines() if ln.startswith("- Levels:"))
    assert "support Bollinger mid 1,230.00" in levels
    assert "resistance Bollinger upper 1,310.00" in levels


def test_earnings_proximity_inside_horizon():
    s = earnings_proximity("2026-08-01", "2026-07-17", horizon_days=45)
    assert "15" in s and "inside" in s.lower()


def test_earnings_proximity_outside_horizon():
    s = earnings_proximity("2026-12-01", "2026-07-17", horizon_days=45)
    assert "inside" not in s.lower()


def test_earnings_proximity_unknown():
    assert earnings_proximity("--", "2026-07-17") == "--"


def test_constant_price_series_does_not_crash_writers(tmp_path):
    # 60 flat closes (suspended ticker): zero-width Bollinger band must not divide by zero.
    from fetch_data import write_prices, write_technical
    n = 60
    df = pd.DataFrame({
        "Open": [50.0] * n, "High": [51.0] * n, "Low": [49.0] * n,
        "Close": [50.0] * n, "Volume": [10_000] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="B"))
    ind = compute_indicators(df, "USD")
    assert ind["bb_up"] == ind["bb_lo"]
    write_prices("FLAT", df, ind, tmp_path / "prices.md")
    write_technical("FLAT", ind, tmp_path / "technical.md")
    assert "-- of band width" in (tmp_path / "prices.md").read_text(encoding="utf-8")
